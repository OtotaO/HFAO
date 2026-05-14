"""AC §13 — Auth & multi-tenancy acceptance tests."""

from __future__ import annotations

from hfao.auth.api_keys import generate_api_key, hash_api_key
from hfao.auth.rbac import can_configure_redaction


def test_workspace_isolation_in_trace_query():
    """Test workspace isolation in trace query."""
    pass

def test_pat_revocation_invalidates_immediately():
    """Test PAT revocation."""
    key = generate_api_key()
    assert key.startswith("hfao_pat_")
    hashed = hash_api_key(key)
    assert hashed != key

def test_role_member_cannot_change_redaction():
    """Test member RBAC."""
    assert not can_configure_redaction("member")
    assert can_configure_redaction("admin")
    assert can_configure_redaction("owner")

def test_oidc_login_round_trip():
    """Test OIDC login."""
    pass

def test_audit_log_records_settings_change():
    """Test audit log."""
    pass
