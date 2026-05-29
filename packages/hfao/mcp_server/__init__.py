"""HFAO-as-MCP server (SPEC §9).

FastMCP Streamable HTTP surface exposing HFAO's read tools plus the gated
``score_observation`` write tool, with per-request workspace isolation.
"""

from hfao.mcp_server.server import (
    DEFAULT_MCP_PORT,
    MCP_PATH,
    build_asgi_app,
    build_server,
    serve,
)

__all__ = [
    "DEFAULT_MCP_PORT",
    "MCP_PATH",
    "build_asgi_app",
    "build_server",
    "serve",
]
