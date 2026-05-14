"""HFAO MCP Tools.

SPEC §9.2. Implements the tool surface.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from hfao.config import HFAOConfig
from hfao.storage.clickhouse_backend import ClickHouseBackend
from hfao.storage.duckdb_backend import DuckDBBackend

logger = logging.getLogger(__name__)


def _open_backend(config: HFAOConfig) -> Any:
    """Open the configured backend for a single handler invocation."""
    if config.backend == "clickhouse":
        if not config.clickhouse_dsn:
            raise ValueError("HFAO_CLICKHOUSE_DSN is required when backend is clickhouse")
        return ClickHouseBackend(config.clickhouse_dsn)
    return DuckDBBackend(config.duckdb_path, readonly=True)


def register_tools(mcp: FastMCP, config: HFAOConfig) -> None:
    """Register all HFAO MCP tools."""

    @mcp.tool()
    def list_traces(project: str, where: str = "1=1", limit: int = 25) -> list[dict[str, Any]]:
        """List summary traces for a project."""
        with _open_backend(config) as backend:
            # TODO: Enforce project scoping
            return backend.list_traces(project, where_sql=where, limit=limit) # type: ignore

    @mcp.tool()
    def get_trace(project: str, trace_id: str) -> dict[str, Any]:
        """Returns observations + scores + causal edges + per-edge confidence."""
        with _open_backend(config) as backend:
            observations = backend.get_trace(project, trace_id) # type: ignore
            scores = backend.get_scores(project, trace_id) # type: ignore
            edges = backend.get_causal_edges(project, trace_id) # type: ignore

        import msgspec

        return {
            "observations": [msgspec.to_builtins(o) for o in observations],
            "scores": [msgspec.to_builtins(s) for s in scores],
            "causal_edges": [msgspec.to_builtins(e) for e in edges],
        }

    @mcp.tool()
    def search_traces(project: str, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Semantic + keyword search over input/output bodies."""
        with _open_backend(config) as backend:
            return backend.search_traces(project, query, limit=limit) # type: ignore

    @mcp.tool()
    def get_causal_attribution(project: str, trace_id: str) -> dict[str, Any]:
        """List decisive-error hypotheses with evidence.
        NOTE: Hypotheses, not verdicts.
        """
        # This will wrap the compute/causal components when they are implemented.
        # For now, it returns a stub or fetches from DB if pre-computed.
        with _open_backend(config) as backend:
            edges = backend.get_causal_edges(project, trace_id) # type: ignore

        import msgspec

        return {
            "trace_id": trace_id,
            "hypotheses": [msgspec.to_builtins(e) for e in edges],
            "replay_supported": False,  # TODO: determine from framework
        }

    @mcp.tool()
    def list_decisive_errors(
        project: str, since: str = "24h", min_confidence: float = 0.3
    ) -> list[dict[str, Any]]:
        """List decisive errors."""
        # For now, return empty.
        return []

    @mcp.tool()
    def compare_runs(project: str, trace_id_a: str, trace_id_b: str) -> dict[str, Any]:
        """Compare two runs."""
        # Fetch both and return a diff/comparison.
        trace_a = get_trace(project, trace_id_a)
        trace_b = get_trace(project, trace_id_b)
        return {
            "trace_id_a": trace_a,
            "trace_id_b": trace_b,
        }
