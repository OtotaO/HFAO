"""RBAC controls."""
from __future__ import annotations

ROLES = ["owner", "admin", "member", "viewer"]

def can_write(role: str) -> bool:
    """Check if role has write permissions."""
    return role in ["owner", "admin", "member"]

def can_configure_redaction(role: str) -> bool:
    """Check if role can configure redaction (admin/owner only)."""
    return role in ["owner", "admin"]
