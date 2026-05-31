"""API Keys auth."""
from __future__ import annotations

import hashlib
import secrets


def generate_api_key() -> str:
    """Generate a new HFAO Personal Access Token."""
    return f"hfao_pat_{secrets.token_urlsafe(32)}"

def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()
