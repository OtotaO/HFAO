"""API token management (SPEC §13.4).

Bearer tokens prefixed ``hfao_pat_``; SHA-256 hashed at rest; scoped to a
workspace + role; rotatable. The storage primitives live in
:mod:`hfao.storage.control_plane`; this module is the **auth-layer surface**
that the SDK, MCP server, and cockpit settings tab use so they never reach
across the auth/storage boundary.

Audit-log emission is wired here (not in the control plane) so issuing or
revoking a key always produces a paper trail without callers having to remember.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hfao.auth.rbac import Permission, Role, require_permission

if TYPE_CHECKING:
    from hfao.storage.control_plane import ControlPlane


@dataclass(frozen=True)
class IssuedKey:
    """Result of :func:`issue`. The raw token is **only** returned once."""

    raw: str
    id: str
    workspace_id: str
    role: Role
    name: str
    prefix: str


class APIKeyError(PermissionError):
    """Raised on invalid / revoked tokens or unauthorized issuance."""


def issue(
    control: ControlPlane,
    *,
    actor_role: Role,
    actor_key_id: str | None,
    workspace_id: str,
    role: Role,
    name: str,
) -> IssuedKey:
    """Issue a new ``hfao_pat_`` token. Records an audit entry.

    ``actor_role`` is the role of the caller; only ``admin`` / ``owner`` may
    issue new keys (§13.2). ``actor_key_id`` is recorded as the audit actor;
    when issuance happens out-of-band (e.g. ``hfao up`` bootstrap), pass
    ``None`` and ``"bootstrap"`` is recorded.
    """
    require_permission(actor_role, Permission.API_KEY_ISSUE)
    raw, meta = control.issue_api_key(
        workspace_id=workspace_id, role=role, name=name
    )
    control.record_audit(
        workspace_id=workspace_id,
        actor=actor_key_id or "bootstrap",
        action=Permission.API_KEY_ISSUE.value,
        target=meta["id"],
        details=json.dumps({"role": role, "name": name, "prefix": meta["prefix"]}),
    )
    return IssuedKey(
        raw=raw,
        id=meta["id"],
        workspace_id=workspace_id,
        role=role,
        name=name,
        prefix=meta["prefix"],
    )


def verify(control: ControlPlane, raw: str) -> dict[str, Any] | None:
    """Return the key row if ``raw`` is a live token, else ``None``.

    Updates ``last_used_at`` as a side effect (delegated to the control plane).
    Revoked keys return ``None`` immediately on the next verify call after the
    revoke commits — the cache is the control-plane row itself, no app-level
    state in front of it.
    """
    return control.verify_api_key(raw)


def revoke(
    control: ControlPlane,
    *,
    actor_role: Role,
    actor_key_id: str | None,
    workspace_id: str,
    key_id: str,
) -> None:
    """Revoke ``key_id``. Requires ``api_key:revoke`` and records an audit row."""
    require_permission(actor_role, Permission.API_KEY_REVOKE)
    control.revoke_api_key(key_id)
    control.record_audit(
        workspace_id=workspace_id,
        actor=actor_key_id or "bootstrap",
        action=Permission.API_KEY_REVOKE.value,
        target=key_id,
        details="{}",
    )


def list_keys(
    control: ControlPlane, *, workspace_id: str
) -> list[dict[str, Any]]:
    """List keys (without the raw token or hash) for a workspace."""
    return control.list_api_keys(workspace_id=workspace_id)


__all__ = ["APIKeyError", "IssuedKey", "issue", "list_keys", "revoke", "verify"]
