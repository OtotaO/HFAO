"""Hugging Face OAuth helper (SPEC §13.3, §10.5).

The HF-Space cockpit shape uses ``gr.LoginButton`` for the actual OAuth dance —
Gradio handles the redirect, code exchange, and cookie. This module is the
**server-side counterpart** that takes the resulting HF access token and
resolves it to a verified user profile via the HF Hub ``/api/whoami-v2``
endpoint.

It's deliberately tiny: we don't reimplement the OAuth flow, we just verify
the token Gradio (or any other HF client) hands us.

Used by:
  - the cockpit ``settings`` tab to display the logged-in HF user;
  - the MCP server, when the deployment opts into HF auth instead of bearer
    PATs (a thin shim that maps the verified HF user to an
    :class:`hfao.mcp_server.auth.Identity`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

HF_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"


class HFOAuthError(RuntimeError):
    """Raised when an HF token is missing, invalid, or HF Hub is unreachable."""


@dataclass(frozen=True)
class HFUser:
    """Subset of the ``whoami-v2`` response we use."""

    name: str
    email: str | None
    is_pro: bool
    orgs: tuple[str, ...]


def verify_token(
    token: str,
    *,
    client: httpx.Client | None = None,
    url: str = HF_WHOAMI_URL,
) -> HFUser:
    """Resolve an HF access token to an :class:`HFUser`.

    ``url`` is parameterised so the AC test can point at a local mock instead
    of hitting the real HF Hub.
    """
    if not token:
        raise HFOAuthError("empty HF token")
    own = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        resp = http.get(url, headers={"Authorization": f"Bearer {token}"})
    finally:
        if own:
            http.close()
    if resp.status_code == 401:
        raise HFOAuthError("HF token rejected (401)")
    if resp.status_code != 200:
        raise HFOAuthError(f"HF whoami failed: HTTP {resp.status_code}")
    body: dict[str, Any] = resp.json()
    orgs_field: list[dict[str, Any]] = list(body.get("orgs") or [])
    org_names = tuple(
        str(o.get("name") or "") for o in orgs_field if o.get("name")
    )
    email_value = body.get("email")
    return HFUser(
        name=str(body.get("name", "")),
        email=str(email_value) if email_value else None,
        is_pro=bool(body.get("isPro", False)),
        orgs=org_names,
    )


__all__ = ["HFUser", "HFOAuthError", "HF_WHOAMI_URL", "verify_token"]
