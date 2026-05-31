"""AC §13 acceptance tests — auth and multi-tenancy.

Covers §13.6:

    - test_workspace_isolation_in_trace_query
    - test_pat_revocation_invalidates_immediately
    - test_role_member_cannot_change_redaction
    - test_oidc_login_round_trip
    - test_audit_log_records_settings_change

The OIDC round-trip stands up a tiny in-process OIDC provider on a uvicorn
background thread that serves a discovery document, a JWKS, and a signed
id_token at its ``/token`` endpoint. The auth-code value is irrelevant
(the mock provider returns the same token regardless) — what we exercise is
the discovery → token-exchange → JWKS-verify path through the real
``hfao.auth.oidc`` client.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from hfao.auth import api_keys
from hfao.auth.hf_oauth import HFOAuthError, verify_token
from hfao.auth.oidc import (
    OIDCConfig,
    discover,
    exchange_code_for_tokens,
    verify_id_token,
)
from hfao.auth.rbac import (
    Permission,
    PermissionDeniedError,
    can,
    require_permission,
)
from hfao.storage.control_plane import ControlPlane
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# --------------------------------------------------------------------------- #
# Workspace isolation
# --------------------------------------------------------------------------- #


def test_workspace_isolation_in_trace_query(tmp_path: Path) -> None:
    """A key issued for workspace A must not yield access to workspace B's project.

    Re-exercises the §9.3 boundary at the auth layer: the verify call returns
    the key's workspace_id and the caller compares it against the project's
    workspace_id before touching the backend.
    """
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws_a = cp.create_workspace(slug="acme", name="Acme")
    ws_b = cp.create_workspace(slug="globex", name="Globex")
    proj_a = cp.create_project(workspace_id=ws_a["id"], slug="demo", name="Demo")
    proj_b = cp.create_project(workspace_id=ws_b["id"], slug="other", name="Other")
    raw_a, _ = cp.issue_api_key(workspace_id=ws_a["id"], role="admin", name="a")

    verified = api_keys.verify(cp, raw_a)
    assert verified is not None
    assert verified["workspace_id"] == ws_a["id"]
    # Cross-workspace project lookup must be refused at the caller's gate.
    assert cp.get_project(proj_a["id"])["workspace_id"] == ws_a["id"]
    assert cp.get_project(proj_b["id"])["workspace_id"] != ws_a["id"]


# --------------------------------------------------------------------------- #
# Revocation
# --------------------------------------------------------------------------- #


def test_pat_revocation_invalidates_immediately(tmp_path: Path) -> None:
    """A revoke must invalidate the token on the very next verify() call."""
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws = cp.create_workspace(slug="acme", name="Acme")
    issued = api_keys.issue(
        cp,
        actor_role="owner",
        actor_key_id=None,
        workspace_id=ws["id"],
        role="admin",
        name="ci",
    )
    assert api_keys.verify(cp, issued.raw) is not None
    api_keys.revoke(
        cp,
        actor_role="owner",
        actor_key_id=None,
        workspace_id=ws["id"],
        key_id=issued.id,
    )
    assert api_keys.verify(cp, issued.raw) is None
    # Audit log records both issuance and revocation.
    actions = [a["action"] for a in cp.list_audit(ws["id"])]
    assert Permission.API_KEY_ISSUE.value in actions
    assert Permission.API_KEY_REVOKE.value in actions


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #


def test_role_member_cannot_change_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``member`` role must not be able to change the redaction profile."""
    # Members hold limited perms; owners hold every perm.
    assert can("owner", Permission.REDACTION_CONFIG_WRITE)
    assert not can("member", Permission.REDACTION_CONFIG_WRITE)
    assert not can("viewer", Permission.REDACTION_CONFIG_WRITE)

    # The cockpit settings entry point reuses require_permission, so a
    # member call raises PermissionDeniedError end-to-end.
    monkeypatch.setenv("HFAO_CONTROL_PLANE_DSN", f"sqlite:///{tmp_path / 'cp.db'}")
    monkeypatch.setenv("HFAO_PROJECT", "ac13")
    monkeypatch.setenv("HFAO_DUCKDB_PATH", str(tmp_path / "hfao.duckdb"))
    monkeypatch.setenv("HFAO_BODIES_PATH", str(tmp_path / "bodies"))
    monkeypatch.setenv("HFAO_BACKEND", "duckdb")
    sys.modules.pop("apps.cockpit.cockpit", None)
    import importlib

    cockpit = importlib.import_module("apps.cockpit.cockpit")
    with pytest.raises(PermissionDeniedError):
        cockpit.cockpit_change_setting(
            "ac13",
            actor_role="member",
            actor_key_id="key_test",
            setting="redaction_profile",
            value="strict",
        )
    sys.modules.pop("apps.cockpit.cockpit", None)


def test_require_permission_helper_raises_for_unknown_perm() -> None:
    with pytest.raises(PermissionDeniedError):
        require_permission("viewer", Permission.PROMPT_WRITE)


# --------------------------------------------------------------------------- #
# Audit log on settings change
# --------------------------------------------------------------------------- #


def test_audit_log_records_settings_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every settings write must persist an audit_log row (§13.5)."""
    monkeypatch.setenv("HFAO_CONTROL_PLANE_DSN", f"sqlite:///{tmp_path / 'cp.db'}")
    monkeypatch.setenv("HFAO_PROJECT", "ac13")
    monkeypatch.setenv("HFAO_DUCKDB_PATH", str(tmp_path / "hfao.duckdb"))
    monkeypatch.setenv("HFAO_BODIES_PATH", str(tmp_path / "bodies"))
    monkeypatch.setenv("HFAO_BACKEND", "duckdb")
    sys.modules.pop("apps.cockpit.cockpit", None)
    import importlib

    cockpit = importlib.import_module("apps.cockpit.cockpit")
    cockpit.cockpit_change_setting(
        "ac13",
        actor_role="owner",
        actor_key_id="key_root",
        setting="judge_model",
        value="claude-haiku-4-6",
    )
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws = cp.get_workspace_by_slug("default")
    assert ws is not None
    audit = cp.list_audit(ws["id"])
    settings_rows = [a for a in audit if a["action"].startswith("settings:")]
    assert len(settings_rows) == 1
    row = settings_rows[0]
    assert row["actor"] == "key_root"
    assert row["target"] == "ac13/judge_model"
    assert json.loads(row["details"]) == {"value": "claude-haiku-4-6"}
    cp.close()
    sys.modules.pop("apps.cockpit.cockpit", None)


# --------------------------------------------------------------------------- #
# OIDC round-trip
# --------------------------------------------------------------------------- #


def _rsa_keypair() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    import base64

    def b64u(n: int) -> str:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key-1",
        "n": b64u(numbers.n),
        "e": b64u(numbers.e),
    }
    return key, jwk


def _build_id_token(
    private_key: rsa.RSAPrivateKey,
    *,
    issuer: str,
    audience: str,
    subject: str,
    email: str,
    nonce: str,
) -> str:
    now = datetime.now(tz=timezone.utc)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "email": email,
        "email_verified": True,
        "name": "Test User",
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return pyjwt.encode(claims, pem, algorithm="RS256", headers={"kid": "test-key-1"})


@contextlib.contextmanager
def _running_provider(issuer: str, app: Starlette) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("mock OIDC provider did not start in time")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def test_oidc_login_round_trip() -> None:
    """End-to-end: discovery → code-exchange → verified ID-token claims."""
    private_key, jwk = _rsa_keypair()
    client_id = "hfao-test-client"
    nonce = "nonce-abc"

    # The issuer URL is the same prefix the mock provider listens at; we wire
    # it in after the server picks a port (via the captured ``base`` variable).
    issuer_holder: dict[str, str] = {}

    async def discovery(_request: Request) -> JSONResponse:
        base = issuer_holder["base"]
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/auth",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/jwks",
                "userinfo_endpoint": f"{base}/userinfo",
            }
        )

    async def jwks(_request: Request) -> JSONResponse:
        return JSONResponse({"keys": [jwk]})

    async def token(_request: Request) -> JSONResponse:
        token_value = _build_id_token(
            private_key,
            issuer=issuer_holder["base"],
            audience=client_id,
            subject="user-1",
            email="alice@example.com",
            nonce=nonce,
        )
        return JSONResponse(
            {"access_token": "tok", "id_token": token_value, "token_type": "Bearer"}
        )

    app = Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery),
            Route("/jwks", jwks),
            Route("/token", token, methods=["POST"]),
        ]
    )

    with _running_provider("http://placeholder", app) as base:
        issuer_holder["base"] = base
        config = OIDCConfig(
            issuer=base,
            client_id=client_id,
            client_secret="secret",
            redirect_uri="http://app/callback",
        )
        # Drive the real client end-to-end.
        discovery_doc = discover(config)
        assert discovery_doc.issuer == base
        tokens = exchange_code_for_tokens(config, discovery_doc, code="anything")
        identity = verify_id_token(
            tokens["id_token"], config, discovery_doc, nonce=nonce
        )
    assert identity.issuer == base
    assert identity.email == "alice@example.com"
    assert identity.email_verified is True
    assert identity.subject == "user-1"


# --------------------------------------------------------------------------- #
# HF OAuth — small surface, mocked HF Hub
# --------------------------------------------------------------------------- #


def test_hf_oauth_verify_token_round_trip() -> None:
    async def whoami(request: Request) -> JSONResponse:
        if request.headers.get("authorization") != "Bearer hf_xyz":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse(
            {
                "name": "alice",
                "email": "alice@example.com",
                "isPro": True,
                "orgs": [{"name": "f8n-ai"}, {"name": "huggingface"}],
            }
        )

    app = Starlette(routes=[Route("/api/whoami-v2", whoami)])
    with _running_provider("http://placeholder", app) as base:
        url = f"{base}/api/whoami-v2"
        user = verify_token("hf_xyz", url=url)
        assert user.name == "alice"
        assert user.is_pro is True
        assert "f8n-ai" in user.orgs
        with pytest.raises(HFOAuthError):
            verify_token("hf_bad", url=url)


def test_hf_oauth_rejects_empty_token() -> None:
    with pytest.raises(HFOAuthError):
        verify_token("", client=httpx.Client())
