"""AC §8 acceptance tests — Stage 2 counterfactual replay (SPEC §8.1, §16 Q-20).

Per the Q-20 caveat, AC tests use a deterministic in-process driver — no
live LLM calls, no live framework invocations. The three Tier-1 framework
drivers (LangGraph / OpenAI Agents SDK / Claude Agent SDK) are exercised via
caller-supplied callables that play the role of the framework's resume
entry point.

Covers:

  - test_pipeline_phase_is_two_in_v0_5_plus
  - test_register_get_list_clear_driver
  - test_rank_for_replay_skips_unsupported_frameworks
  - test_rank_for_replay_skips_when_no_driver_registered
  - test_stage2_emits_decisive_error_edge_on_flip
  - test_stage2_omits_edge_when_replay_does_not_flip
  - test_stage2_records_driver_errors_without_crashing
  - test_stage2_returns_empty_when_no_candidates
  - test_attribute_failure_runs_stage2_and_persists_edges
  - test_attribute_failure_stage2_does_not_break_static
  - test_langgraph_driver_uses_thread_id
  - test_openai_agents_driver_uses_run_state
  - test_claude_agent_driver_uses_session_id
  - test_drivers_missing_metadata_return_driver_error
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hfao.compute.causal.counterfactual import (
    DeterministicReplayDriver,
    Stage2Result,
    clear_drivers,
    get_driver,
    list_drivers,
    rank_for_replay,
    register_driver,
    stage2_counterfactual,
)
from hfao.compute.causal.judge import DeterministicJudge
from hfao.compute.causal.pipeline import PHASE, attribute_failure
from hfao.config import HFAOConfig
from hfao.instrumentations.claude_agent_extra import ClaudeAgentReplayDriver
from hfao.instrumentations.langgraph_extra import LangGraphReplayDriver
from hfao.instrumentations.openai_agents_extra import OpenAIAgentsReplayDriver
from hfao.schema.causal import CausalEdge
from hfao.schema.events import (
    CostBreakdown,
    Observation,
    ObservationType,
    Status,
    TokenUsage,
)
from hfao.storage.duckdb_backend import DuckDBBackend

_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_driver_registry() -> Iterator[None]:
    """Every test starts with an empty registry and leaves it empty."""
    clear_drivers()
    yield
    clear_drivers()


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[DuckDBBackend]:
    b = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    b.init_schema()
    yield b
    b.close()


def _obs(
    *,
    obs_id: str,
    framework: str | None = "langgraph",
    extra_metadata: dict[str, str] | None = None,
    status: Status = "error",
    obs_type: ObservationType = "AGENT",
    start_offset_ms: int = 0,
) -> Observation:
    metadata: dict[str, str] = {}
    if framework is not None:
        metadata["framework"] = framework
    if framework == "langgraph":
        metadata["langgraph.thread_id"] = f"thread-{obs_id}"
    if framework == "openai_agents":
        metadata["openai_agents.run_state"] = (
            '{"resumable": true, "obs": "' + obs_id + '"}'
        )
    if framework == "claude_agent_sdk":
        metadata["claude_agent_sdk.session_id"] = f"session-{obs_id}"
    if extra_metadata:
        metadata.update(extra_metadata)
    start = _NOW + timedelta(milliseconds=start_offset_ms)
    return Observation(
        project_id="p1",
        trace_id="t1",
        observation_id=obs_id,
        name=f"{framework or 'unknown'}.step",
        type=obs_type,
        start_time=start,
        end_time=start + timedelta(milliseconds=50),
        duration_ms=50,
        ingested_at=start,
        status=status,
        metadata=metadata,
        usage=TokenUsage(),
        cost=CostBreakdown(),
        event_version=1,
    )


def _hint(target_id: str, confidence: float = 0.8) -> CausalEdge:
    return CausalEdge(
        project_id="p1",
        trace_id="t1",
        source_observation_id="o0",
        target_observation_id=target_id,
        edge_type="DECISIVE_ERROR",
        confidence=confidence,
        method="LLM_JUDGE",
        evidence="static + judge hint",
        replay_supported=True,
        computed_at=_NOW,
    )


# --------------------------------------------------------------------------- #
# Pipeline phase
# --------------------------------------------------------------------------- #


def test_pipeline_phase_is_two_in_v0_5_plus() -> None:
    """Q-20 flipped PHASE to 2 so the pipeline runs Stage 2 automatically."""
    assert PHASE == 2


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_register_get_list_clear_driver() -> None:
    driver = DeterministicReplayDriver(framework="langgraph")
    assert list_drivers() == []
    register_driver(driver)
    assert list_drivers() == ["langgraph"]
    assert get_driver("LangGraph") is driver  # case-insensitive lookup
    # Re-registering replaces.
    other = DeterministicReplayDriver(framework="langgraph")
    register_driver(other)
    assert get_driver("langgraph") is other
    clear_drivers()
    assert list_drivers() == []
    assert get_driver("langgraph") is None
    assert get_driver(None) is None


# --------------------------------------------------------------------------- #
# Candidate ranking
# --------------------------------------------------------------------------- #


def test_rank_for_replay_skips_unsupported_frameworks() -> None:
    register_driver(DeterministicReplayDriver(framework="langgraph"))
    lg = _obs(obs_id="lg1")
    crew = _obs(obs_id="crew1", framework="crewai")
    candidates = rank_for_replay([lg, crew], [_hint("lg1"), _hint("crew1")])
    assert [c.observation.observation_id for c in candidates] == ["lg1"]


def test_rank_for_replay_skips_when_no_driver_registered() -> None:
    """Even replay-supported frameworks are skipped when no driver is wired."""
    lg = _obs(obs_id="lg1")
    candidates = rank_for_replay([lg], [_hint("lg1")])
    assert candidates == []


def test_rank_for_replay_dedups_on_target() -> None:
    register_driver(DeterministicReplayDriver(framework="langgraph"))
    lg = _obs(obs_id="lg1")
    # Two hints pointing at the same target — should produce one candidate.
    candidates = rank_for_replay([lg], [_hint("lg1", 0.6), _hint("lg1", 0.9)])
    assert len(candidates) == 1
    # Highest-confidence hint wins the candidate slot.
    assert candidates[0].hint_confidence == 0.9


def test_rank_for_replay_honours_max_candidates() -> None:
    register_driver(DeterministicReplayDriver(framework="langgraph"))
    obs = [_obs(obs_id=f"lg{i}") for i in range(6)]
    hints = [_hint(o.observation_id, confidence=0.9 - 0.01 * i) for i, o in enumerate(obs)]
    candidates = rank_for_replay(obs, hints, max_candidates=3)
    assert len(candidates) == 3


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def test_stage2_emits_decisive_error_edge_on_flip() -> None:
    register_driver(
        DeterministicReplayDriver(framework="langgraph", flips={"lg1": True})
    )
    obs = [_obs(obs_id="lg1")]
    result = stage2_counterfactual(obs, [_hint("lg1")])
    assert result.candidates_evaluated == 1
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.edge_type == "DECISIVE_ERROR"
    assert edge.method == "COUNTERFACTUAL_REPLAY"
    assert edge.source_observation_id == "lg1"
    assert edge.target_observation_id == "lg1"
    assert edge.confidence == 0.97
    assert edge.replay_supported is True


def test_stage2_omits_edge_when_replay_does_not_flip() -> None:
    register_driver(
        DeterministicReplayDriver(framework="langgraph", flips={})
    )
    obs = [_obs(obs_id="lg1")]
    result = stage2_counterfactual(obs, [_hint("lg1")])
    assert result.candidates_evaluated == 1
    assert result.edges == []


def test_stage2_records_driver_errors_without_crashing() -> None:
    register_driver(
        DeterministicReplayDriver(
            framework="langgraph", raises_for={"lg1"}
        )
    )
    obs = [_obs(obs_id="lg1")]
    result = stage2_counterfactual(obs, [_hint("lg1")])
    assert result.edges == []
    assert any("lg1" in err for err in result.driver_errors)


def test_stage2_returns_empty_when_no_candidates() -> None:
    """No drivers registered + replay-supported framework → empty result."""
    obs = [_obs(obs_id="lg1")]
    result = stage2_counterfactual(obs, [_hint("lg1")])
    assert result.edges == []
    assert result.candidates_evaluated == 0


def test_stage2_skips_when_driver_cannot_replay() -> None:
    """Driver registered but `can_replay` returns False → skipped."""
    register_driver(DeterministicReplayDriver(framework="langgraph"))
    # No `langgraph.thread_id` metadata so the deterministic driver's
    # can_replay returns False.
    obs = [_obs(obs_id="lg1", extra_metadata={"langgraph.thread_id": ""})]
    # Strip the auto-injected thread_id by re-creating with no extras then
    # blanking metadata entirely.
    obs[0].metadata.pop("langgraph.thread_id", None)
    result = stage2_counterfactual(obs, [_hint("lg1")])
    # The deterministic driver's can_replay returns False → skipped.
    assert result.edges == []
    assert result.candidates_skipped_unsupported >= 1


# --------------------------------------------------------------------------- #
# Pipeline integration
# --------------------------------------------------------------------------- #


def test_attribute_failure_runs_stage2_and_persists_edges(
    backend: DuckDBBackend,
) -> None:
    """End-to-end: pipeline runs Stage 1 + Stage 3 + Stage 2 and persists."""
    register_driver(
        DeterministicReplayDriver(framework="langgraph", flips={"lg1": True})
    )
    backend.write_events([_obs(obs_id="lg1")])
    result = attribute_failure(
        backend,
        project_id="p1",
        trace_id="t1",
        config=HFAOConfig(),
        judge=DeterministicJudge(),
    )
    assert result.stage2_evaluated >= 1
    assert result.stage2_flipped >= 1
    # The persisted DECISIVE_ERROR edge exists with the replay method.
    persisted = backend.get_causal_edges("p1", "t1")
    assert any(
        e.method == "COUNTERFACTUAL_REPLAY" and e.confidence == 0.97
        for e in persisted
    )


def test_attribute_failure_stage2_does_not_break_static(
    backend: DuckDBBackend,
) -> None:
    """A failing driver leaves Stage 1 + Stage 3 edges intact."""
    register_driver(
        DeterministicReplayDriver(framework="langgraph", raises_for={"lg1"})
    )
    backend.write_events([_obs(obs_id="lg1")])
    result = attribute_failure(
        backend,
        project_id="p1",
        trace_id="t1",
        config=HFAOConfig(),
        judge=DeterministicJudge(),
    )
    # Stage 2 produced no flipped edges but the pipeline still returns
    # something + the static/judge edges still landed.
    assert result.stage2_flipped == 0
    assert result.static_edge_count + result.judge_edge_count >= 1


def test_attribute_failure_runs_when_no_driver_registered(
    backend: DuckDBBackend,
) -> None:
    """No registered driver → Stage 2 is a quiet no-op. Pipeline still runs."""
    backend.write_events([_obs(obs_id="lg1")])
    result = attribute_failure(
        backend,
        project_id="p1",
        trace_id="t1",
        config=HFAOConfig(),
        judge=DeterministicJudge(),
    )
    assert result.stage2_flipped == 0
    assert result.stage2_evaluated == 0
    # Stage 3 / Stage 1 still ran.
    assert len(result.edges) >= 1


# --------------------------------------------------------------------------- #
# Tier-1 framework drivers
# --------------------------------------------------------------------------- #


def test_langgraph_driver_uses_thread_id() -> None:
    """LangGraph driver: calls graph_factory and treats no-error result as flip."""
    calls: list[tuple[dict, dict]] = []

    class FakeGraph:
        def invoke(self, state: dict, *, config: dict) -> dict:
            calls.append((state, config))
            return {"output": "ok"}

    driver = LangGraphReplayDriver(graph_factory=lambda: FakeGraph())
    obs = _obs(obs_id="lg1")
    outcome = driver.replay(trace=[obs], candidate=obs)
    assert outcome.framework == "langgraph"
    assert outcome.flipped is True
    assert outcome.driver_error is None
    assert calls[0][1] == {"configurable": {"thread_id": "thread-lg1"}}


def test_langgraph_driver_treats_error_result_as_no_flip() -> None:
    class ErrGraph:
        def invoke(self, state: dict, *, config: dict) -> dict:
            del state, config
            return {"error": "still failing"}

    driver = LangGraphReplayDriver(graph_factory=lambda: ErrGraph())
    obs = _obs(obs_id="lg1")
    outcome = driver.replay(trace=[obs], candidate=obs)
    assert outcome.flipped is False
    assert outcome.driver_error is None


def test_openai_agents_driver_uses_run_state() -> None:
    """Agents driver: calls resume() with the run_state, flips on final_output."""
    seen: list[str] = []

    def resume(state: str, perturb: dict) -> dict:
        del perturb
        seen.append(state)
        return {"final_output": "answered"}

    driver = OpenAIAgentsReplayDriver(resume=resume)
    obs = _obs(obs_id="oa1", framework="openai_agents")
    outcome = driver.replay(trace=[obs], candidate=obs)
    assert outcome.flipped is True
    assert "oa1" in seen[0]


def test_openai_agents_driver_no_resume_returns_driver_error() -> None:
    driver = OpenAIAgentsReplayDriver()  # no resume wired
    obs = _obs(obs_id="oa1", framework="openai_agents")
    outcome = driver.replay(trace=[obs], candidate=obs)
    assert outcome.flipped is False
    assert outcome.driver_error == "missing_resume"


def test_claude_agent_driver_uses_session_id() -> None:
    """Claude Agent SDK driver: passes session_id to resume_from()."""
    calls: list[str] = []

    def resume_from(session_id: str) -> dict:
        calls.append(session_id)
        return {"status": "completed"}

    driver = ClaudeAgentReplayDriver(resume_from=resume_from)
    obs = _obs(obs_id="ca1", framework="claude_agent_sdk")
    outcome = driver.replay(trace=[obs], candidate=obs)
    assert outcome.flipped is True
    assert calls == ["session-ca1"]


def test_claude_agent_driver_failure_does_not_crash() -> None:
    def boom(_session_id: str) -> dict:
        raise RuntimeError("SDK unreachable")

    driver = ClaudeAgentReplayDriver(resume_from=boom)
    obs = _obs(obs_id="ca1", framework="claude_agent_sdk")
    outcome = driver.replay(trace=[obs], candidate=obs)
    assert outcome.flipped is False
    assert outcome.driver_error == "SDK unreachable"


def test_drivers_missing_metadata_return_driver_error() -> None:
    """Each driver returns a driver_error when the candidate lacks its key."""
    lg = LangGraphReplayDriver(graph_factory=lambda: object())
    no_thread = _obs(obs_id="lg1")
    no_thread.metadata.pop("langgraph.thread_id")
    out = lg.replay(trace=[no_thread], candidate=no_thread)
    assert out.driver_error == "missing_thread_id"

    oa = OpenAIAgentsReplayDriver(resume=lambda s, p: {"final_output": "x"})
    no_state = _obs(obs_id="oa1", framework="openai_agents")
    no_state.metadata.pop("openai_agents.run_state")
    out = oa.replay(trace=[no_state], candidate=no_state)
    assert out.driver_error == "missing_run_state"

    ca = ClaudeAgentReplayDriver(resume_from=lambda s: {"status": "ok"})
    no_session = _obs(obs_id="ca1", framework="claude_agent_sdk")
    no_session.metadata.pop("claude_agent_sdk.session_id")
    out = ca.replay(trace=[no_session], candidate=no_session)
    assert out.driver_error == "missing_session_id"


def test_install_replay_driver_registers_each_framework() -> None:
    """The convenience installers register under the right framework key."""
    from hfao.instrumentations.claude_agent_extra import (
        install_replay_driver as install_ca,
    )
    from hfao.instrumentations.langgraph_extra import (
        install_replay_driver as install_lg,
    )
    from hfao.instrumentations.openai_agents_extra import (
        install_replay_driver as install_oa,
    )

    install_lg(graph_factory=lambda: None)
    install_oa(resume=lambda s, p: None)
    install_ca(resume_from=lambda s: None)
    assert set(list_drivers()) == {
        "langgraph",
        "openai_agents",
        "claude_agent_sdk",
    }


def test_stage2_result_dataclass_defaults() -> None:
    """An empty Stage2Result has empty edges and zero counts."""
    r = Stage2Result()
    assert r.edges == []
    assert r.candidates_evaluated == 0
    assert r.candidates_skipped_unsupported == 0
    assert r.driver_errors == []
