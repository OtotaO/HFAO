"""HF OAuth flow."""
from __future__ import annotations


def exchange_hf_token(token: str) -> dict[str, str]:
    """Exchange a Hugging Face token for an HFAO session."""
    # Stub implementation
    return {"workspace": "hf_user", "role": "member"}
