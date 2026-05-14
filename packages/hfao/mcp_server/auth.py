"""HFAO MCP Auth.

SPEC §9.3. Authentication for MCP server.
Three modes:
1. API key (default for self-host)
2. HF OAuth (HF Spaces deploys)
3. OIDC (enterprise)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Stub for Week 6 auth implementations.
# This will be flushed out when implementing the "Auth functionality" step of the plan.

class AuthProvider:
    def authenticate(self, request: Any) -> dict[str, Any]:
        """Authenticate a request and return context (e.g. project constraints)."""
        return {}
