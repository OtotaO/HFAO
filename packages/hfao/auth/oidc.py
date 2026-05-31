"""Generic OIDC auth."""
from __future__ import annotations

from hfao.config import HFAOConfig


def verify_oidc_token(token: str, config: HFAOConfig) -> dict[str, str]:
    """Verify an OIDC token using configured provider."""
    # Stub implementation
    return {"workspace": "oidc_user", "role": "member"}
