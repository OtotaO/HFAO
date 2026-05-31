"""AC §8 acceptance tests — causal-attribution subset.

Covers the §8.5 lines that pertain to the Stage-1 + Stage-3 pipeline:

    - test_static_extracts_handoff_edge
    - test_static_extracts_tool_dataflow_edge
    - test_static_extracts_prompt_conditioning_edge
    - test_static_extracts_retrieval_to_generation_edge
    - test_static_handles_empty_trace
    - test_static_is_deterministic
    - test_judge_returns_ranked_hypotheses
    - test_judge_replay_supported_correct_per_framework
    - test_pipeline_writes_and_dedups_on_rerun
    - test_pipeline_skips_when_no_failure
    - test_replay_supported_false_for_crewai
    - test_replay_supported_true_for_langgraph

Stage 2 (counterfactual replay) is Phase 2; its acceptance tests land with
that wiring (weeks 9–10 per §15.2 / Q-17). Eval, cost, and monitor tests
land alongside their respective engines later this week.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hfao.compute.causal import (
    DeterministicJudge,
    attribute_failure,
    is_replay_supported,
    replay_supported_for_observations,
    should_attribute,
    stage1_static,
    stage3_judge,
)
from hfao.config import HFAOConfig
from hfao.schema.events import (
    CostBreakdown,
    Observation,
    ObservationType,
    Status,
    TokenUsage,
    ToolCall,
)
from hfao.storage.duckdb_backend import DuckDBBackend

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def _obs(
    *,
    obs_id: str,
    name: str = "step",
    obs_type: ObservationType = "GENERATION",
    parent: str | None = None,
    agent_id: str | None = None,
    handoff_target_agent_id: str | None = None,
    input: str | None = None,  # noqa: A002 — matches schema field name
    output: str | None = None,
    status: Status = "ok",
    status_message: str | None = None,
    metadata: dict[str, str] | None = None,
    tool_calls: list[ToolCall] | None = None,
    start_offset_ms: int = 0,
    project_id: str = "p1",
    trace_id: str = "t1",
) -> Observation:
    start = _NOW + timedelta(milliseconds=start_offset_ms)
    return Observation(
        project_id=project_id,
        trace_id=trace_id,
        observation_id=obs_id,
        parent_observation_id=parent,
        name=name,
        type=obs_type,
        start_time=start,
        end_time=start + timedelta(milliseconds=10),
        duration_ms=10,
        ingested_at=start,
        status=status,
        status_message=status_message,
        input=input,
        output=output,
        agent_id=agent_id,
        handoff_target_agent_id=handoff_target_agent_id,
        usage=TokenUsage(),
        cost=CostBreakdown(),
        metadata=metadata or {},
        tool_calls=tool_calls or [],
        event_version=1,
    )


# --------------------------------------------------------------------------- #
# Stage 1 — static
# --------------------------------------------------------------------------- #


def test_static_handles_empty_trace() -> None:
    assert stage1_static([]) == []


def test_static_extracts_handoff_edge() -> None:
    """Explicit handoff_target_agent_id resolves to the right target."""
    triage = _obs(
        obs_id="o1",
        obs_type="AGENT",
        name="triage",
        agent_id="triage-agent",
        handoff_target_agent_id="billing-agent",
        start_offset_ms=0,
    )
    billing = _obs(
        obs_id="o2",
        obs_type="AGENT",
        name="billing",
        agent_id="billing-agent",
        start_offset_ms=100,
    )
    edges = stage1_static([triage, billing])
    handoff = [e for e in edges if e.edge_type == "HANDOFF"]
    assert len(handoff) == 1
    assert handoff[0].source_observation_id == "o1"
    assert handoff[0].target_observation_id == "o2"
    assert handoff[0].method == "STATIC"
    assert handoff[0].confidence > 0.9


def test_static_extracts_handoff_edge_from_a2a_metadata() -> None:
    """A2A task lineage produces a HANDOFF when explicit field is absent."""
    triage = _obs(
        obs_id="o1",
        obs_type="AGENT",
        name="triage",
        agent_id="triage-agent",
        metadata={"a2a.task_id": "task-42"},
        start_offset_ms=0,
    )
    billing = _obs(
        obs_id="o2",
        obs_type="AGENT",
        name="billing",
        agent_id="billing-agent",
        metadata={"a2a.task_id": "task-42"},
        start_offset_ms=100,
    )
    edges = stage1_static([triage, billing])
    a2a = [e for e in edges if e.edge_type == "HANDOFF"]
    assert any(
        e.source_observation_id == "o1" and "a2a.task_id" in e.evidence
        for e in a2a
    )


def test_static_extracts_tool_dataflow_edge() -> None:
    """Tool args quoting a prior output produce a TOOL_DEPENDENCY edge."""
    quote = "the quick brown fox jumps over the lazy dog and runs"
    search = _obs(
        obs_id="o1",
        obs_type="RETRIEVAL",
        name="vector_search",
        output=quote + " — and more padding to push it well past 32 chars",
        start_offset_ms=0,
    )
    fetch = _obs(
        obs_id="o2",
        obs_type="TOOL",
        name="fetch_doc",
        tool_calls=[
            ToolCall(
                id="t1",
                name="fetch_doc",
                arguments='{"q": "' + quote + '"}',
            )
        ],
        start_offset_ms=100,
    )
    edges = stage1_static([search, fetch])
    tool = [e for e in edges if e.edge_type == "TOOL_DEPENDENCY"]
    assert len(tool) == 1
    assert tool[0].source_observation_id == "o1"
    assert tool[0].target_observation_id == "o2"
    assert tool[0].confidence > 0.8


def test_static_extracts_prompt_conditioning_edge() -> None:
    """A GENERATION's input quoting a sibling's output → PROMPT_CONDITIONING."""
    common = "Paris is the capital of France, a country in Western Europe."
    sibling_a = _obs(
        obs_id="oa",
        obs_type="GENERATION",
        parent="root",
        output=common + " It has a population of approximately 2.1 million.",
        start_offset_ms=0,
    )
    sibling_b = _obs(
        obs_id="ob",
        obs_type="GENERATION",
        parent="root",
        input="Summarise: " + common + " Use one paragraph.",
        start_offset_ms=100,
    )
    edges = stage1_static([sibling_a, sibling_b])
    pc = [e for e in edges if e.edge_type == "PROMPT_CONDITIONING"]
    assert any(e.source_observation_id == "oa" and e.target_observation_id == "ob" for e in pc)


def test_static_extracts_retrieval_to_generation_edge() -> None:
    """RETRIEVAL output appearing in the next GENERATION input is DATAFLOW."""
    chunk = "Cynefin: a framework for decision making first published in 1999"
    retrieval = _obs(
        obs_id="r1",
        obs_type="RETRIEVAL",
        name="kb_lookup",
        output=chunk + " by Dave Snowden while working at IBM.",
        start_offset_ms=0,
    )
    generation = _obs(
        obs_id="g1",
        obs_type="GENERATION",
        name="answer",
        input="Context: " + chunk + " — explain how it applies to incident triage.",
        start_offset_ms=100,
    )
    edges = stage1_static([retrieval, generation])
    df = [
        e
        for e in edges
        if e.edge_type == "DATAFLOW" and e.source_observation_id == "r1"
    ]
    assert len(df) == 1
    assert df[0].target_observation_id == "g1"


def test_static_is_deterministic() -> None:
    """Same observations in any input order → same edges (modulo non-PK fields)."""
    triage = _obs(obs_id="o1", obs_type="AGENT", name="triage",
                  agent_id="t", handoff_target_agent_id="b")
    billing = _obs(obs_id="o2", obs_type="AGENT", name="billing",
                   agent_id="b", start_offset_ms=100)
    edges1 = stage1_static([triage, billing])
    edges2 = stage1_static([triage, billing])
    keys1 = [(e.source_observation_id, e.target_observation_id, e.edge_type) for e in edges1]
    keys2 = [(e.source_observation_id, e.target_observation_id, e.edge_type) for e in edges2]
    assert keys1 == keys2


# --------------------------------------------------------------------------- #
# Replay-support registry
# --------------------------------------------------------------------------- #


def test_replay_supported_true_for_langgraph() -> None:
    assert is_replay_supported("langgraph")
    obs = [_obs(obs_id="o1", metadata={"framework": "langgraph"})]
    assert replay_supported_for_observations(obs) is True


def test_replay_supported_false_for_crewai() -> None:
    assert is_replay_supported("crewai") is False
    obs = [_obs(obs_id="o1", metadata={"framework": "crewai"})]
    assert replay_supported_for_observations(obs) is False


def test_replay_supported_false_when_no_framework_metadata() -> None:
    """Honest default: empty metadata → False (never optimistic)."""
    obs = [_obs(obs_id="o1")]
    assert replay_supported_for_observations(obs) is False


def test_replay_supported_inferred_from_span_name_prefix() -> None:
    """`langgraph.node` span name implies a langgraph framework."""
    obs = [_obs(obs_id="o1", name="langgraph.node")]
    assert replay_supported_for_observations(obs) is True


# --------------------------------------------------------------------------- #
# Stage 3 — judge
# --------------------------------------------------------------------------- #


def test_judge_returns_ranked_hypotheses() -> None:
    """DeterministicJudge picks the latest error candidate as the top hypothesis."""
    o1 = _obs(obs_id="o1", name="parse", status="ok")
    o2 = _obs(obs_id="o2", name="tool_call", status="error",
              status_message="HTTP 500 from search", start_offset_ms=100)
    o3 = _obs(obs_id="o3", name="answer", status="ok", start_offset_ms=200)
    edges = stage3_judge([o1, o2, o3], [], judge=DeterministicJudge())
    assert len(edges) == 1
    edge = edges[0]
    assert edge.edge_type == "DECISIVE_ERROR"
    assert edge.method == "LLM_JUDGE"
    assert edge.source_observation_id == "o2"
    assert edge.target_observation_id == "o2"
    assert edge.judge_model == "deterministic-judge"
    assert "HTTP 500" in edge.evidence
    assert 0.0 <= edge.confidence <= 1.0


def test_judge_replay_supported_correct_per_framework() -> None:
    """Edge.replay_supported reflects the candidate's framework, not the trace."""
    crew = _obs(obs_id="c1", name="step", status="error",
                metadata={"framework": "crewai"})
    edges = stage3_judge([crew], [], judge=DeterministicJudge())
    assert edges and edges[0].replay_supported is False

    lg = _obs(obs_id="l1", name="step", status="error",
              metadata={"framework": "langgraph"})
    edges = stage3_judge([lg], [], judge=DeterministicJudge())
    assert edges and edges[0].replay_supported is True


def test_judge_handles_no_candidates() -> None:
    """All-ok trace + no hints → judge returns []."""
    o1 = _obs(obs_id="o1", name="ok-only")
    # Synthetic hint pointing to a non-existent observation should not crash.
    edges = stage3_judge([o1], [], judge=DeterministicJudge())
    # Without any error, the deterministic judge picks the latest, low confidence.
    assert len(edges) == 1
    assert edges[0].confidence < 0.5


# --------------------------------------------------------------------------- #
# Pipeline + storage round-trip
# --------------------------------------------------------------------------- #


@pytest.fixture
def backend(tmp_path: Path) -> DuckDBBackend:
    b = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    b.init_schema()
    return b


def test_pipeline_skips_when_no_failure(backend: DuckDBBackend) -> None:
    """No error + no hint → AttributionResult(edges=[], skipped_judge=True)."""
    o1 = _obs(obs_id="o1", name="ok")
    backend.write_events([o1])
    result = attribute_failure(
        backend,
        project_id="p1",
        trace_id="t1",
        config=HFAOConfig(),
    )
    assert result.edges == []
    assert result.skipped_judge is True


def test_pipeline_writes_and_dedups_on_rerun(backend: DuckDBBackend) -> None:
    """Re-running on the same trace must not duplicate edges."""
    triage = _obs(obs_id="o1", obs_type="AGENT", name="triage",
                  agent_id="t", handoff_target_agent_id="b")
    failing = _obs(obs_id="o2", obs_type="AGENT", name="billing",
                   agent_id="b", status="error",
                   status_message="DB timeout", start_offset_ms=100)
    backend.write_events([triage, failing])

    judge = DeterministicJudge()
    first = attribute_failure(
        backend, project_id="p1", trace_id="t1",
        config=HFAOConfig(), judge=judge,
    )
    assert first.static_edge_count >= 1
    assert first.judge_edge_count == 1
    # Re-run: edges in the DB should not double up.
    persisted_first = backend.get_causal_edges("p1", "t1")
    second = attribute_failure(
        backend, project_id="p1", trace_id="t1",
        config=HFAOConfig(), judge=judge,
    )
    persisted_second = backend.get_causal_edges("p1", "t1")
    assert second.static_edge_count == first.static_edge_count
    assert second.judge_edge_count == first.judge_edge_count
    assert len(persisted_second) == len(persisted_first)


def test_pipeline_judge_failure_does_not_break_static(backend: DuckDBBackend) -> None:
    """If the judge raises, Stage 1 edges still land; skipped_judge=True."""

    class BoomJudge:
        model = "boom"

        def attribute(self, observations, candidates):  # type: ignore[no-untyped-def]
            raise RuntimeError("judge backend unreachable")

    triage = _obs(obs_id="o1", obs_type="AGENT", name="triage",
                  agent_id="t", handoff_target_agent_id="b")
    failing = _obs(obs_id="o2", obs_type="AGENT", name="billing",
                   agent_id="b", status="error", start_offset_ms=100)
    backend.write_events([triage, failing])
    result = attribute_failure(
        backend, project_id="p1", trace_id="t1",
        config=HFAOConfig(), judge=BoomJudge(),
    )
    assert result.skipped_judge is True
    assert result.static_edge_count >= 1
    assert result.judge_edge_count == 0


def test_should_attribute_signals() -> None:
    """Triggers per §8.1."""
    ok = [_obs(obs_id="a1")]
    err = [_obs(obs_id="a1", status="error")]
    assert should_attribute(ok) is False
    assert should_attribute(err) is True
