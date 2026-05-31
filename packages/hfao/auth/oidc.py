"""Generic OIDC client (SPEC §13.3).

Env-driven by ``HFAO_OIDC_ISSUER_URL`` / ``HFAO_OIDC_CLIENT_ID`` /
``HFAO_OIDC_CLIENT_SECRET`` (Appendix A). The flow:

    1. discovery — fetch ``{issuer}/.well-known/openid-configuration``;
    2. authorization URL — build the redirect URL for the user agent;
    3. token exchange — POST ``code`` to the token endpoint;
    4. ID-token verification — RS256 against the JWKS endpoint.

The verified claims are then mapped to an :class:`OIDCIdentity` that callers
(cockpit settings tab, MCP HTTP front) translate into an
:class:`hfao.mcp_server.auth.Identity` by resolving the email's workspace.

This module is **transport-agnostic**: it never starts an HTTP server or
mutates state on disk. It calls out only via ``httpx`` (sync), so tests can
inject a custom client or hit a local mock issuer.

SAML is out of scope for v1 per §13.3 (enterprise / Phase 2+).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from msgspec import Struct


class OIDCError(RuntimeError):
    """Raised on discovery / token-exchange / verification failure."""


@dataclass(frozen=True)
class OIDCDiscovery:
    """Subset of the OIDC discovery document we actually consume."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None


class OIDCConfig(Struct, frozen=True, kw_only=True):
    """Minimal config to talk to an OIDC provider."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")


@dataclass(frozen=True)
class OIDCIdentity:
    """Verified subject + email + name from a validated ID token."""

    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    name: str | None


def discover(config: OIDCConfig, *, client: httpx.Client | None = None) -> OIDCDiscovery:
    """Fetch the OIDC discovery document for ``config.issuer``."""
    url = config.issuer.rstrip("/") + "/.well-known/openid-configuration"
    return _discover(url, client)


def _discover(url: str, client: httpx.Client | None) -> OIDCDiscovery:
    own = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        resp = http.get(url)
        if resp.status_code != 200:
            raise OIDCError(f"discovery failed: HTTP {resp.status_code}")
        doc: dict[str, Any] = resp.json()
    finally:
        if own:
            http.close()
    try:
        return OIDCDiscovery(
            issuer=str(doc["issuer"]),
            authorization_endpoint=str(doc["authorization_endpoint"]),
            token_endpoint=str(doc["token_endpoint"]),
            jwks_uri=str(doc["jwks_uri"]),
            userinfo_endpoint=(
                str(doc["userinfo_endpoint"]) if doc.get("userinfo_endpoint") else None
            ),
        )
    except KeyError as exc:
        raise OIDCError(f"discovery doc missing required field: {exc}") from exc


def make_state() -> str:
    """Generate a URL-safe CSRF state token."""
    return secrets.token_urlsafe(24)


def make_nonce() -> str:
    """Generate a nonce for replay-attack defence."""
    return secrets.token_urlsafe(24)


def build_authorization_url(
    config: OIDCConfig, discovery: OIDCDiscovery, *, state: str, nonce: str
) -> str:
    """Build the authorization URL the user agent is redirected to."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
        "nonce": nonce,
    }
    return f"{discovery.authorization_endpoint}?{urlencode(params)}"


def exchange_code_for_tokens(
    config: OIDCConfig,
    discovery: OIDCDiscovery,
    *,
    code: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Exchange the auth code for ``id_token`` / ``access_token``."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    own = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        resp = http.post(discovery.token_endpoint, data=payload)
    finally:
        if own:
            http.close()
    if resp.status_code != 200:
        raise OIDCError(
            f"token exchange failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    tokens: dict[str, Any] = resp.json()
    if "id_token" not in tokens:
        raise OIDCError("token response did not include id_token")
    return tokens


def _jwks_client_for(jwks_uri: str) -> pyjwt.PyJWKClient:
    # PyJWKClient caches JWKS lookups internally; cache the client instance
    # per URI so we don't re-construct it for every verification.
    return _cached_jwks_client(jwks_uri)


@lru_cache(maxsize=8)
def _cached_jwks_client(jwks_uri: str) -> pyjwt.PyJWKClient:
    return pyjwt.PyJWKClient(jwks_uri, cache_keys=True)


def verify_id_token(
    id_token: str,
    config: OIDCConfig,
    discovery: OIDCDiscovery,
    *,
    nonce: str | None = None,
) -> OIDCIdentity:
    """Verify ``id_token`` against the JWKS and return the identity claims.

    Enforces signature, ``iss``, ``aud``, ``exp``, and (when provided) ``nonce``.
    """
    try:
        key = _jwks_client_for(discovery.jwks_uri).get_signing_key_from_jwt(id_token)
    except pyjwt.PyJWKClientError as exc:
        raise OIDCError(f"jwks lookup failed: {exc}") from exc
    try:
        claims: dict[str, Any] = pyjwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=config.client_id,
            issuer=discovery.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except pyjwt.PyJWTError as exc:
        raise OIDCError(f"id_token verification failed: {exc}") from exc
    if nonce is not None and claims.get("nonce") != nonce:
        raise OIDCError("id_token nonce mismatch (possible replay)")
    return OIDCIdentity(
        issuer=str(claims["iss"]),
        subject=str(claims["sub"]),
        email=claims.get("email"),
        email_verified=bool(claims.get("email_verified", False)),
        name=claims.get("name"),
    )


__all__ = [
    "OIDCConfig",
    "OIDCDiscovery",
    "OIDCError",
    "OIDCIdentity",
    "build_authorization_url",
    "discover",
    "exchange_code_for_tokens",
    "make_nonce",
    "make_state",
    "verify_id_token",
]
