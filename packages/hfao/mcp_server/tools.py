# pyright: reportUnusedFunction=false
# ^ The tool/resource/prompt closures below are registered by the FastMCP
#   decorators' side effects, not referenced by name; pyright's unused-function
#   check is a false positive for this DI-by-closure registration pattern.
"""HFAO MCP tool surface.

SPEC §9.2. Every read tool wraps the :class:`~hfao.storage.StorageBackend`
protocol; the one write tool (``score_observation``) is gated by
``HFAO_MCP_READ_ONLY`` (§9.1). Workspace isolation is enforced per call through
:func:`hfao.mcp_server.auth.authorize_project` (§9.3).

Tools return JSON-able builtins (``dict`` / ``list[dict]``) rather than msgspec
Structs so the FastMCP serializer stays backend-agnostic; this matches the
``dict[str, Any]`` convention the storage protocol already uses.

``run_eval`` invokes :mod:`hfao.compute.eval.runner` directly. The Week-8
experiment runner (§15.2; §16 Q-17) extends this to multi-variant tournaments
without changing the §9.2 tool signature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import msgspec

from hfao.mcp_server.auth import AuthError, authorize_project, require_identity
from hfao.schema.scores import Score

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from hfao.config import HFAOConfig
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane

# Honest framing required by §8.1 / Appendix C rule 2 and §9.2.
HYPOTHESIS_DISCLAIMER = (
    "These are ranked hypotheses produced by static analysis and/or an LLM "
    "judge, not verdicts. Weigh each by its confidence, method, and evidence."
)

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_WINDOW_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_window(window: str) -> timedelta:
    """Parse a window like ``"5m"``, ``"24h"``, ``"7d"`` into a timedelta."""
    match = _WINDOW_RE.match(window)
    if not match:
        raise ValueError(f"invalid window: {window!r} (expected e.g. 5m, 24h, 7d)")
    qty, unit = int(match.group(1)), match.group(2)
    return timedelta(**{_WINDOW_UNITS[unit]: qty})


def _builtins(value: Any) -> Any:
    """Convert msgspec Structs (and nested) to JSON-able builtins."""
    return msgspec.to_builtins(value)


@dataclass(frozen=True)
class Deps:
    """Everything the tools need, injected once at server construction."""

    backend: StorageBackend
    control: ControlPlane
    config: HFAOConfig


def register_tools(mcp: FastMCP, deps: Deps) -> None:  # noqa: C901 - one flat surface
    """Register the full §9.2 tool / resource / prompt surface on ``mcp``."""
    backend = deps.backend
    control = deps.control

    @mcp.tool
    def list_traces(
        project: str, where: str = "1=1", limit: int = 25
    ) -> list[dict[str, Any]]:
        """List trace summaries for a project. ``where`` is a SQL WHERE
        fragment; the backend enforces project scoping."""
        authorize_project(control, project)
        return backend.list_traces(project, where_sql=where, limit=limit)

    @mcp.tool
    def get_trace(project: str, trace_id: str) -> dict[str, Any]:
        """Return a trace's observations + scores + causal edges."""
        authorize_project(control, project)
        return {
            "trace_id": trace_id,
            "observations": [_builtins(o) for o in backend.get_trace(project, trace_id)],
            "scores": [_builtins(s) for s in backend.get_scores(project, trace_id)],
            "causal_edges": [
                _builtins(e) for e in backend.get_causal_edges(project, trace_id)
            ],
        }

    @mcp.tool
    def search_traces(
        project: str, query: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Keyword search over trace input/output bodies and span names."""
        authorize_project(control, project)
        return backend.search_traces_text(project, query, limit=limit)

    @mcp.tool
    def get_causal_attribution(project: str, trace_id: str) -> dict[str, Any]:
        """Ranked decisive-error hypotheses with evidence and a
        ``replay_supported`` flag per edge. NOTE: hypotheses, not verdicts."""
        authorize_project(control, project)
        edges = backend.get_causal_edges(project, trace_id)
        hypotheses = sorted(
            (_builtins(e) for e in edges),
            key=lambda e: e["confidence"],
            reverse=True,
        )
        return {
            "trace_id": trace_id,
            "disclaimer": HYPOTHESIS_DISCLAIMER,
            "hypotheses": hypotheses,
        }

    @mcp.tool
    def list_decisive_errors(
        project: str, since: str = "24h", min_confidence: float = 0.3
    ) -> list[dict[str, Any]]:
        """Recent DECISIVE_ERROR edges across the project, ranked by confidence."""
        authorize_project(control, project)
        cutoff = datetime.now(timezone.utc) - parse_window(since)
        sql = (
            "SELECT trace_id, source_observation_id, target_observation_id, "
            "confidence, method, evidence, replay_supported, computed_at "
            "FROM causal_edges "
            "WHERE edge_type = 'DECISIVE_ERROR' "
            f"AND confidence >= {float(min_confidence)} "
            f"AND computed_at >= TIMESTAMP '{cutoff:%Y-%m-%d %H:%M:%S}' "
            "ORDER BY confidence DESC"
        )
        return backend.execute_readonly_sql(project, sql)

    @mcp.tool
    def compare_runs(
        project: str, trace_id_a: str, trace_id_b: str
    ) -> dict[str, Any]:
        """Side-by-side comparison of two traces (spans, cost, status, scores)."""
        authorize_project(control, project)

        def _summary(tid: str) -> dict[str, Any]:
            obs = backend.get_trace(project, tid)
            scores = backend.get_scores(project, tid)
            return {
                "trace_id": tid,
                "span_count": len(obs),
                "total_cost_usd": sum(o.cost.total_cost_usd for o in obs),
                "total_tokens": sum(o.usage.total_tokens for o in obs),
                "has_error": any(o.status == "error" for o in obs),
                "scores": {s.name: s.value for s in scores},
            }

        return {"a": _summary(trace_id_a), "b": _summary(trace_id_b)}

    @mcp.tool
    def run_eval(
        project: str,
        dataset: str,
        evaluators: list[str],
        runtime_url: str | None = None,
    ) -> dict[str, Any]:
        """Launch an offline eval run against a dataset.

        Calls :func:`hfao.compute.eval.runner.run_eval`, which iterates the
        dataset, invokes the runtime (HTTP if ``runtime_url`` is provided,
        echo runtime otherwise), runs each evaluator, and persists scores
        with the new ``eval_run_id`` so the cockpit Evals tab + MCP queries
        can aggregate them.
        """
        authorize_project(control, project)
        from hfao.compute.eval.runner import run_eval as _run_eval

        return _run_eval(
            project=project,
            dataset=dataset,
            evaluators=evaluators,
            runtime_url=runtime_url,
            backend=backend,
            control=control,
        )

    @mcp.tool
    def get_prompt(
        project: str, name: str, label: str = "production"
    ) -> dict[str, Any]:
        """Resolve a prompt by label (default ``production``)."""
        authorize_project(control, project)
        prompt = control.get_prompt(project_id=project, name=name, label=label)
        if prompt is None:
            raise ValueError(f"no prompt {name!r} with label {label!r} in {project}")
        return prompt

    @mcp.tool
    def list_prompts(project: str) -> list[dict[str, Any]]:
        """List the latest version of every prompt in a project."""
        authorize_project(control, project)
        return control.list_prompts(project_id=project)

    @mcp.tool
    def score_observation(
        project: str,
        trace_id: str,
        observation_id: str | None,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Attach a human/agent annotation score. Gated by HFAO_MCP_READ_ONLY."""
        if deps.config.mcp_read_only:
            raise AuthError("server is in read-only mode (HFAO_MCP_READ_ONLY=true)")
        identity = authorize_project(control, project)
        score = Score(
            project_id=project,
            trace_id=trace_id,
            observation_id=observation_id,
            name=name,
            value=value,
            source="ANNOTATION",
            comment=comment,
            timestamp=datetime.now(timezone.utc),
            annotator_id=identity.key_id,
        )
        backend.write_scores([score])
        control.record_audit(
            workspace_id=identity.workspace_id,
            actor=identity.key_id,
            action="score_observation",
            target=f"{project}/{trace_id}/{observation_id or ''}",
            details=msgspec.json.encode({"name": name, "value": value}).decode(),
        )
        return _builtins(score)

    @mcp.tool
    def get_cost_by(
        project: str, group_by: list[str], window: str = "7d"
    ) -> list[dict[str, Any]]:
        """Cost rollup grouped by any subset of
        (date, user_id, agent_id, model, prompt_name)."""
        authorize_project(control, project)
        now = datetime.now(timezone.utc)
        return backend.cost_rollup(
            project,
            date_from=now - parse_window(window),
            date_to=now,
            group_by=group_by,
        )

    @mcp.tool
    def list_insights(
        project: str,
        since: str = "7d",
        min_severity: str = "info",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Detected anomalies + replay-verified causes for ``project`` (§16 Q-18).

        ``since`` is a window like ``"24h"`` / ``"7d"`` matching the existing
        cost-by window grammar. ``min_severity`` ∈ ``{info, notice, warning,
        critical}`` and filters by the severity ladder.
        """
        authorize_project(control, project)
        since_iso = (datetime.now(timezone.utc) - parse_window(since)).isoformat()
        return control.list_insights(
            project_id=project,
            since=since_iso,
            min_severity=min_severity,
            limit=limit,
        )

    @mcp.tool
    def get_insight(project: str, insight_id: str) -> dict[str, Any]:
        """Fetch one insight by id."""
        authorize_project(control, project)
        return control.get_insight(project_id=project, insight_id=insight_id)

    @mcp.resource("hfao://traces/{project}/{trace_id}")
    def trace_resource(project: str, trace_id: str) -> str:
        """Expose a trace as a readable JSON resource."""
        authorize_project(control, project)
        payload = {
            "observations": [_builtins(o) for o in backend.get_trace(project, trace_id)],
            "scores": [_builtins(s) for s in backend.get_scores(project, trace_id)],
            "causal_edges": [
                _builtins(e) for e in backend.get_causal_edges(project, trace_id)
            ],
        }
        return msgspec.json.encode(payload).decode()

    @mcp.prompt
    def explain_failure(project: str, trace_id: str) -> str:
        """Return a templated prompt the client LLM can use to explain a failure."""
        require_identity(control)
        return (
            f"You are debugging agent trace {trace_id} in project {project}.\n"
            f"Use the `get_trace` and `get_causal_attribution` tools to gather "
            f"evidence. {HYPOTHESIS_DISCLAIMER}\n"
            f"Then explain, step by step, the most likely decisive error and the "
            f"smallest change that would have prevented it."
        )


__all__ = ["HYPOTHESIS_DISCLAIMER", "Deps", "parse_window", "register_tools"]
