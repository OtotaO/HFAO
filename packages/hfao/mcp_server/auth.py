"""MCP server authentication and workspace resolution.

SPEC §9.3. Three auth modes:

1. **API key** (default for self-host): ``Authorization: Bearer hfao_pat_...``.
2. **HTTP Basic** ``(workspace_slug, api_key)``.
3. **OIDC** (enterprise) — handled by the generic OIDC layer in ``hfao.auth``;
   the resolved identity arrives here the same way an API key does.

Every tool resolves the caller's workspace from the bound identity and refuses
to read or write any project that does not belong to that workspace
(workspace isolation, §9.3). Identity is carried in a :class:`contextvars.ContextVar`
so the same code path serves both the ASGI middleware (one identity per HTTP
request) and in-memory test calls (identity bound for the duration of a block).
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Iterator
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

from fastmcp.server.dependencies import get_http_headers
from msgspec import Struct

if TYPE_CHECKING:
    from hfao.storage.control_plane import ControlPlane


class Identity(Struct, frozen=True, kw_only=True):
    """An authenticated caller, scoped to exactly one workspace."""

    workspace_id: str
    workspace_slug: str
    role: str
    key_id: str


_CURRENT: ContextVar[Identity | None] = ContextVar("hfao_mcp_identity", default=None)


def current_identity() -> Identity | None:
    """Return the identity bound to the current context, if any."""
    return _CURRENT.get()


@contextlib.contextmanager
def bind_identity(identity: Identity | None) -> Iterator[None]:
    """Bind ``identity`` for the duration of the block (ASGI request or test)."""
    token: Token[Identity | None] = _CURRENT.set(identity)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _resolve_key(control: ControlPlane, raw_key: str) -> Identity | None:
    row = control.verify_api_key(raw_key)
    if row is None:
        return None
    workspace = control.get_workspace(row["workspace_id"])
    return Identity(
        workspace_id=row["workspace_id"],
        workspace_slug=workspace["slug"],
        role=row["role"],
        key_id=row["id"],
    )


def resolve_identity(
    control: ControlPlane,
    *,
    authorization: str | None = None,
) -> Identity | None:
    """Resolve an :class:`Identity` from an ``Authorization`` header value.

    Supports ``Bearer hfao_pat_...`` and ``Basic base64(slug:key)``. Returns
    ``None`` when the header is missing or the credentials are invalid; callers
    translate ``None`` into an authentication error.
    """
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    scheme = scheme.strip().lower()
    rest = rest.strip()
    if scheme == "bearer":
        return _resolve_key(control, rest)
    if scheme == "basic":
        try:
            decoded = base64.b64decode(rest).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        slug, _, key = decoded.partition(":")
        identity = _resolve_key(control, key)
        # The slug must match the workspace the key belongs to (§9.3).
        if identity is None or identity.workspace_slug != slug:
            return None
        return identity
    return None


class AuthError(PermissionError):
    """Raised when a tool is called without a valid, authorized identity."""


def resolve_current_identity(control: ControlPlane) -> Identity | None:
    """Resolve the caller's identity for the current tool invocation.

    A contextvar override (set via :func:`bind_identity`) wins when present —
    this is used by same-task in-process callers and unit tests. Otherwise the
    identity is resolved from the current HTTP request's ``Authorization``
    header, which FastMCP propagates into the tool task over Streamable HTTP.
    """
    override = current_identity()
    if override is not None:
        return override
    try:
        headers = get_http_headers(include_all=True)
    except RuntimeError:
        headers = {}
    return resolve_identity(control, authorization=headers.get("authorization"))


def require_identity(control: ControlPlane) -> Identity:
    """Return the resolved identity or raise :class:`AuthError`."""
    identity = resolve_current_identity(control)
    if identity is None:
        raise AuthError("authentication required: present a valid hfao_pat_ API key")
    return identity


def authorize_project(control: ControlPlane, project_id: str) -> Identity:
    """Ensure the bound identity may access ``project_id``.

    Enforces workspace isolation: the project must belong to the identity's
    workspace. Raises :class:`AuthError` otherwise.
    """
    identity = require_identity(control)
    try:
        project = control.get_project(project_id)
    except KeyError as exc:
        raise AuthError(f"unknown project: {project_id}") from exc
    if project["workspace_id"] != identity.workspace_id:
        raise AuthError(
            f"project {project_id} is not in workspace {identity.workspace_slug}"
        )
    return identity


__all__ = [
    "AuthError",
    "Identity",
    "authorize_project",
    "bind_identity",
    "current_identity",
    "require_identity",
    "resolve_current_identity",
    "resolve_identity",
]
