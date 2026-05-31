"""HFAO-as-MCP Server.

SPEC §9.1. Provides a FastMCP Streamable HTTP server at `:4319/mcp`.
Read-only mode can be toggled via `HFAO_MCP_READ_ONLY=true`.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from hfao.config import HFAOConfig

logger = logging.getLogger(__name__)


def create_mcp_server(config: HFAOConfig) -> FastMCP:
    """Create the HFAO MCP server instance."""
    name = "hfao-mcp"
    if config.mcp_read_only:
        name += " (read-only)"

    mcp = FastMCP(name)

    # Register tools
    from hfao.mcp_server import tools

    tools.register_tools(mcp, config)

    return mcp


def run(config: HFAOConfig | None = None) -> None:
    """Run the MCP server."""
    if config is None:
        config = HFAOConfig.from_env()

    mcp = create_mcp_server(config)

    # We use stdio for the agent to connect, or SSE for a standalone server.
    # We'll run SSE on port 4319.
    host = "0.0.0.0"
    port = 4319
    logger.info(
        f"Starting HFAO MCP server at {host}:{port}/mcp (read_only={config.mcp_read_only})"
    )

    try:
        # Note: FastMCP run() uses stdio by default unless configured otherwise.
        # But for the HTTP Streamable requirement:
        from fastmcp.server import create_sse_app
        import uvicorn

        app: object = create_sse_app(mcp)
        uvicorn.run(app, host=host, port=port) # type: ignore
    except ImportError:
        logger.warning("uvicorn not installed, falling back to stdio FastMCP transport")
        mcp.run()
