"""Causal-attribution pipeline (SPEC §8.1).

Orchestrates Stage 1 (static dataflow) + Stage 3 (LLM judge) on a single
trace and persists the resulting :class:`CausalEdge` records via the storage
backend. Stage 2 (counterfactual replay) is Phase 2 — the gate exists here so
the wiring is ready for week-11, but it does not run in v1.

Triggers per §8.1:

  * any score with ``name == "success"`` and ``value < 0.5``;
  * any observation with ``status == "error"``;
  * a manual ``attribute_failure(...)`` call.

The pipeline is **safe to re-run** on the same trace: the backend's
``write_causal_edges`` uses the ``(project_id, trace_id, source, target,
method)`` primary key with ``ReplacingMergeTree``-style dedup, so re-runs
update edges rather than duplicate them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from hfao.compute.causal.judge import Judge, select_judge, stage3_judge
from hfao.compute.causal.static import stage1_static

if TYPE_CHECKING:
    from hfao.config import HFAOConfig
    from hfao.schema.causal import CausalEdge
    from hfao.schema.events import Observation
    from hfao.storage import StorageBackend


PHASE: Literal[1, 2] = 1


@dataclass(frozen=True)
class AttributionResult:
    """What :func:`attribute_failure` returns."""

    trace_id: str
    edges: list[CausalEdge]
    static_edge_count: int
    judge_edge_count: int
    skipped_judge: bool


def should_attribute(observations: list[Observation]) -> bool:
    """Return True iff the trace looks like a failure worth attributing.

    Failure signals per §8.1:
      * any observation has ``status == "error"``, or
      * a success score < 0.5 exists (callers can pre-filter).
    """
    return any(o.status == "error" for o in observations)


def attribute_failure(
    backend: StorageBackend,
    *,
    project_id: str,
    trace_id: str,
    config: HFAOConfig,
    judge: Judge | None = None,
    persist: bool = True,
    force: bool = False,
) -> AttributionResult:
    """Run the pipeline against an existing trace.

    Parameters
    ----------
    backend
        Storage backend implementing the ``StorageBackend`` protocol; used
        for read (``get_trace``) and write (``write_causal_edges``).
    project_id, trace_id
        Identify the trace to attribute.
    config
        Used to construct the judge when ``judge`` is not supplied.
    judge
        Optional explicit judge. Useful for tests (``DeterministicJudge``) and
        for callers that want to share a configured client across many calls.
    persist
        When ``True`` (default), write the resulting edges back via the
        backend. Tests pass ``False`` to inspect the result without DB
        side effects.
    force
        When ``True``, run the pipeline even if :func:`should_attribute`
        returns ``False``. Manual triggers from the cockpit and MCP use this.
    """
    observations = backend.get_trace(project_id, trace_id)
    if not observations:
        return AttributionResult(
            trace_id=trace_id,
            edges=[],
            static_edge_count=0,
            judge_edge_count=0,
            skipped_judge=True,
        )
    if not force and not should_attribute(observations):
        return AttributionResult(
            trace_id=trace_id,
            edges=[],
            static_edge_count=0,
            judge_edge_count=0,
            skipped_judge=True,
        )

    static_edges = stage1_static(observations)
    judge_obj = judge if judge is not None else select_judge(config)

    judge_edges: list[CausalEdge] = []
    skipped_judge = False
    try:
        judge_edges = stage3_judge(observations, static_edges, judge=judge_obj)
    except Exception:  # noqa: BLE001 — judge failures must not break the pipeline
        # We silently skip Stage 3 on any judge error (network, API, JSON
        # parse). Stage 1 edges still land. The caller can detect this via
        # ``skipped_judge`` and (re-)run with a different judge.
        skipped_judge = True

    edges = list(static_edges) + list(judge_edges)
    if persist and edges:
        backend.write_causal_edges(edges)
    return AttributionResult(
        trace_id=trace_id,
        edges=edges,
        static_edge_count=len(static_edges),
        judge_edge_count=len(judge_edges),
        skipped_judge=skipped_judge,
    )


__all__ = [
    "PHASE",
    "AttributionResult",
    "attribute_failure",
    "should_attribute",
]
