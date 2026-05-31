"""Auth and multi-tenancy surface (SPEC §13).

  - :mod:`hfao.auth.rbac`      — roles, permissions, ``require_permission``
  - :mod:`hfao.auth.api_keys`  — issue / verify / revoke ``hfao_pat_`` tokens
  - :mod:`hfao.auth.oidc`      — generic OIDC discovery / token exchange / JWT verify
  - :mod:`hfao.auth.hf_oauth`  — Hugging Face Hub token verification

Storage and audit live in :mod:`hfao.storage.control_plane`; this package is
the auth-layer surface other modules consume.
"""

from hfao.auth import api_keys, hf_oauth, oidc, rbac
from hfao.auth.rbac import (
    Permission,
    PermissionDeniedError,
    Role,
    can,
    permissions_for,
    require_permission,
    role_rank,
)

__all__ = [
    "Permission",
    "PermissionDeniedError",
    "Role",
    "api_keys",
    "can",
    "hf_oauth",
    "oidc",
    "permissions_for",
    "rbac",
    "require_permission",
    "role_rank",
]
