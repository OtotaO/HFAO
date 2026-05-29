"""HFAO-as-MCP server.

SPEC §9.1. FastMCP Streamable HTTP at ``:4319/mcp``. Each tool resolves the
caller's identity from the current request's ``Authorization`` header (which
FastMCP propagates into the tool task) via :mod:`hfao.mcp_server.auth`, then
enforces workspace isolation (§9.3).

``build_server`` returns the configured :class:`fastmcp.FastMCP` instance;
``build_asgi_app`` returns the Streamable-HTTP Starlette app (with the FastMCP
lifespan that initialises the session manager); ``serve`` runs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP

from hfao.mcp_server.tools import Deps, register_tools

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from hfao.config import HFAOConfig
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane

MCP_PATH = "/mcp"
DEFAULT_MCP_PORT = 4319

INSTRUCTIONS = (
    "HFAO — Hugging Face Agent Observatory. Query agent traces, scores, costs, "
    "and causal-attribution hypotheses. Causal edges are hypotheses, not "
    "verdicts: always weigh confidence, method, and evidence."
)


def build_server(
    config: HFAOConfig, backend: StorageBackend, control: ControlPlane
) -> FastMCP:
    """Construct a FastMCP server with the full §9.2 tool surface registered."""
    mcp: FastMCP = FastMCP(name="hfao", instructions=INSTRUCTIONS)
    register_tools(mcp, Deps(backend=backend, control=control, config=config))
    return mcp


def build_asgi_app(
    config: HFAOConfig, backend: StorageBackend, control: ControlPlane
) -> Starlette:
    """Build the Streamable-HTTP ASGI app (includes the FastMCP lifespan)."""
    mcp = build_server(config, backend, control)
    return mcp.http_app(path=MCP_PATH, transport="http")


def serve(
    config: HFAOConfig,
    backend: StorageBackend,
    control: ControlPlane,
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_MCP_PORT,
) -> None:  # pragma: no cover - exercised by deployment, not unit tests
    """Run the MCP server over Streamable HTTP at ``{host}:{port}{MCP_PATH}``."""
    import uvicorn

    app = build_asgi_app(config, backend, control)
    uvicorn.run(app, host=host, port=port)


__all__ = [
    "DEFAULT_MCP_PORT",
    "MCP_PATH",
    "build_asgi_app",
    "build_server",
    "serve",
]
