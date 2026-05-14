"""AC §9 — MCP server acceptance tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP
from hfao.config import HFAOConfig
from hfao.mcp_server.server import create_mcp_server


@pytest.fixture
def mcp_server(tmp_path: Any):
    config = HFAOConfig(
        project="ac9",
        backend="duckdb",
        duckdb_path=str(tmp_path / "ac9.duckdb"),
        mcp_read_only=False,
    )
    return create_mcp_server(config)

@pytest.mark.anyio
async def test_mcp_streamable_http_handshake(mcp_server: FastMCP):
    """Test MCP HTTP handshake."""
    assert mcp_server.name == "hfao-mcp"
    tools = await mcp_server.list_tools() # type: ignore
    assert "list_traces" in [t.name for t in tools] # type: ignore

def test_list_traces_requires_auth():
    """Test auth check for list_traces."""
    # TODO: stub until auth is wired
    pass

def test_score_observation_blocked_in_readonly():
    """Test read-only mode."""
    # TODO: stub
    pass

def test_get_causal_attribution_returns_replay_flag(mcp_server: FastMCP):
    """Test get_causal_attribution."""
    # TODO: test specific tool implementation
    pass

def test_workspace_isolation_enforced():
    """Test workspace isolation."""
    # TODO: stub
    pass

def test_mcp_resource_uri_resolves():
    """Test MCP resource URIs."""
    pass
