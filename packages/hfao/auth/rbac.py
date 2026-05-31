"""Role-based access control (SPEC §13.2).

Four roles — ``owner``, ``admin``, ``member``, ``viewer`` — gated against a
fixed permission set. The map is exhaustive and lives in this file so any
future role / permission change has exactly one place to land. Other modules
(MCP, cockpit settings, eval engine) call :func:`require_permission` to
enforce.

Roles bracket what an :class:`hfao.mcp_server.auth.Identity` may do. Workspace
isolation is enforced separately (an identity may only act inside its own
workspace, regardless of role).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from msgspec import Struct

Role = Literal["owner", "admin", "member", "viewer"]
_ROLE_ORDER: tuple[Role, ...] = ("viewer", "member", "admin", "owner")


class Permission(str, Enum):
    """Permissions referenced across §6, §8, §10, §13.

    The string values double as machine-readable identifiers used by the
    audit log (``action`` column) so they survive across processes.
    """

    PROJECT_CREATE = "project:create"
    PROJECT_DELETE = "project:delete"
    PROMPT_WRITE = "prompt:write"
    DATASET_WRITE = "dataset:write"
    ANNOTATION_WRITE = "annotation:write"
    EVAL_RUN = "eval:run"
    MONITOR_WRITE = "monitor:write"
    REDACTION_CONFIG_WRITE = "redaction:config:write"
    SSO_CONFIG_WRITE = "sso:config:write"
    SETTINGS_WRITE = "settings:write"
    API_KEY_ISSUE = "api_key:issue"
    API_KEY_REVOKE = "api_key:revoke"
    SCORE_WRITE = "score:write"
    TRACE_READ = "trace:read"
    TRACE_DELETE = "trace:delete"


# Cumulative permission grants per role (each role inherits everything below).
_ROLE_GRANTS: dict[Role, frozenset[Permission]] = {
    "viewer": frozenset({Permission.TRACE_READ}),
    "member": frozenset(
        {
            Permission.TRACE_READ,
            Permission.SCORE_WRITE,
            Permission.ANNOTATION_WRITE,
            Permission.DATASET_WRITE,
            Permission.EVAL_RUN,
        }
    ),
    "admin": frozenset(
        {
            Permission.TRACE_READ,
            Permission.SCORE_WRITE,
            Permission.ANNOTATION_WRITE,
            Permission.DATASET_WRITE,
            Permission.EVAL_RUN,
            Permission.PROMPT_WRITE,
            Permission.MONITOR_WRITE,
            Permission.PROJECT_CREATE,
            Permission.API_KEY_ISSUE,
            Permission.API_KEY_REVOKE,
            Permission.TRACE_DELETE,
            Permission.SETTINGS_WRITE,
        }
    ),
    "owner": frozenset(p for p in Permission),  # every permission
}


class PermissionDeniedError(PermissionError):
    """Raised when an :class:`Identity` lacks the required permission."""


class RoleIdentity(Struct, frozen=True, kw_only=True):
    """A minimal identity shape accepted by the permission helpers.

    Both :class:`hfao.mcp_server.auth.Identity` and ad-hoc test identities are
    structurally compatible — they all carry a ``role`` field. Using a
    duck-typed shape keeps ``rbac`` import-light (no MCP server dependency).
    """

    role: Role


def permissions_for(role: Role) -> frozenset[Permission]:
    """Return the full permission set granted to ``role``."""
    return _ROLE_GRANTS[role]


def can(role: Role, permission: Permission) -> bool:
    """Predicate form of :func:`require_permission`."""
    return permission in _ROLE_GRANTS[role]


def require_permission(role: Role, permission: Permission) -> None:
    """Raise :class:`PermissionDeniedError` unless ``role`` grants ``permission``."""
    if not can(role, permission):
        raise PermissionDeniedError(
            f"role {role!r} lacks permission {permission.value!r}"
        )


def role_rank(role: Role) -> int:
    """Position in the privilege order (viewer=0 ≤ … ≤ owner=3)."""
    return _ROLE_ORDER.index(role)


__all__ = [
    "Permission",
    "PermissionDeniedError",
    "Role",
    "RoleIdentity",
    "can",
    "permissions_for",
    "require_permission",
    "role_rank",
]
