"""Stage 1 — static dataflow extraction (SPEC §8.1).

Builds a directed multigraph of :class:`CausalEdge` records from a single
trace's observations, using only static lexical/structural analysis (no
LLM, no replay). The five edge classes per §8.1:

  * **parent/child span** — always emitted, low base confidence.
  * **HANDOFF** — explicit ``handoff_target_agent_id`` set, or the OTel-GenAI
    handoff operation, or A2A task lineage from ``metadata['a2a.task_id']``.
  * **TOOL_DEPENDENCY** — tool args contain a substring (``≥ MIN_LEN`` chars)
    of a prior observation's output.
  * **PROMPT_CONDITIONING** — a ``GENERATION``'s input contains a prior
    sibling's output.
  * **DATAFLOW (retrieval→generation)** — a ``RETRIEVAL``'s output appears in
    the next ``GENERATION``'s input.

Tunables per §8.1:

  * ``MIN_LEN`` — substring match length, 32 chars.
  * ``MAX_LOOKBACK_SPANS`` — cap pairwise comparisons, 50.
  * ``MIN_SIM`` (cosine, optional embedding pass) — 0.85; not used at Stage 1
    unless an embedding hook is wired (Phase 2).

Output is deterministic given an input observation list: edges are returned
sorted by ``(source_observation_id, target_observation_id, edge_type)``.

This module is **pure** — no I/O, no network, no storage. The pipeline
(:mod:`hfao.compute.causal.pipeline`) is responsible for persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from hfao.compute.causal.replay_support import replay_supported_for_observations
from hfao.schema.causal import CausalEdge

if TYPE_CHECKING:
    from hfao.schema.events import Observation, ToolCall

# Tunables (§8.1)
MIN_LEN = 32
MAX_LOOKBACK_SPANS = 50

# Base confidence by edge type. Calibrated so the most structurally clear
# signals score highest; LLM-judge edges land later with their own scores.
_CONF_PARENT = 0.30
_CONF_HANDOFF = 0.95
_CONF_HANDOFF_DERIVED = 0.75  # derived from A2A metadata, not explicit field
_CONF_TOOL_DEPENDENCY = 0.85
_CONF_PROMPT_CONDITIONING = 0.70
_CONF_RETRIEVAL_TO_GEN = 0.80


def stage1_static(observations: list[Observation]) -> list[CausalEdge]:
    """Return all static causal edges for ``observations``.

    Empty input → empty output. Observations are processed in their given
    order; the caller is responsible for sorting by ``start_time`` if a
    chronological pass matters (the parent/child relations don't depend on it).
    """
    if not observations:
        return []
    project_id = observations[0].project_id
    trace_id = observations[0].trace_id
    replay = replay_supported_for_observations(observations)
    now = datetime.now(timezone.utc)
    edges: list[CausalEdge] = []

    edges.extend(_parent_child(observations, project_id, trace_id, replay, now))
    edges.extend(_handoffs(observations, project_id, trace_id, replay, now))
    edges.extend(_tool_dependency(observations, project_id, trace_id, replay, now))
    edges.extend(
        _prompt_conditioning(observations, project_id, trace_id, replay, now)
    )
    edges.extend(
        _retrieval_to_generation(observations, project_id, trace_id, replay, now)
    )

    # Stable sort for deterministic test assertions + reproducible writes.
    edges.sort(
        key=lambda e: (e.source_observation_id, e.target_observation_id, e.edge_type)
    )
    return edges


# --------------------------------------------------------------------------- #


def _parent_child(
    obs: list[Observation],
    project_id: str,
    trace_id: str,
    replay: bool,
    now: datetime,
) -> list[CausalEdge]:
    out: list[CausalEdge] = []
    for o in obs:
        if o.parent_observation_id:
            out.append(
                CausalEdge(
                    project_id=project_id,
                    trace_id=trace_id,
                    source_observation_id=o.parent_observation_id,
                    target_observation_id=o.observation_id,
                    edge_type="DATAFLOW",
                    confidence=_CONF_PARENT,
                    method="STATIC",
                    evidence="parent/child span relation from OTLP span graph",
                    replay_supported=replay,
                    computed_at=now,
                )
            )
    return out


def _handoffs(
    obs: list[Observation],
    project_id: str,
    trace_id: str,
    replay: bool,
    now: datetime,
) -> list[CausalEdge]:
    """Two paths: explicit ``handoff_target_agent_id`` + A2A task lineage."""
    out: list[CausalEdge] = []
    by_agent: dict[str, str] = {}
    for o in obs:
        if o.agent_id:
            by_agent.setdefault(o.agent_id, o.observation_id)
    for o in obs:
        target_agent = o.handoff_target_agent_id
        if target_agent and target_agent in by_agent:
            out.append(
                CausalEdge(
                    project_id=project_id,
                    trace_id=trace_id,
                    source_observation_id=o.observation_id,
                    target_observation_id=by_agent[target_agent],
                    edge_type="HANDOFF",
                    confidence=_CONF_HANDOFF,
                    method="STATIC",
                    evidence=(
                        f"handoff_target_agent_id={target_agent!r} resolved to "
                        f"observation {by_agent[target_agent]!r}"
                    ),
                    replay_supported=replay,
                    computed_at=now,
                )
            )
    out.extend(_a2a_handoffs(obs, project_id, trace_id, replay, now))
    return out


def _a2a_handoffs(
    obs: list[Observation],
    project_id: str,
    trace_id: str,
    replay: bool,
    now: datetime,
) -> list[CausalEdge]:
    """Group spans by A2A task_id; chain them in start_time order."""
    by_task: dict[str, list[Observation]] = {}
    for o in obs:
        task = o.metadata.get("a2a.task_id") if o.metadata else None
        if task:
            by_task.setdefault(task, []).append(o)
    out: list[CausalEdge] = []
    for task, members in by_task.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda o: o.start_time)
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if prev.agent_id == nxt.agent_id:
                # Same agent within a task is not a handoff.
                continue
            out.append(
                CausalEdge(
                    project_id=project_id,
                    trace_id=trace_id,
                    source_observation_id=prev.observation_id,
                    target_observation_id=nxt.observation_id,
                    edge_type="HANDOFF",
                    confidence=_CONF_HANDOFF_DERIVED,
                    method="STATIC",
                    evidence=(
                        f"A2A task lineage: shared a2a.task_id={task!r} across "
                        f"distinct agents {prev.agent_id!r} → {nxt.agent_id!r}"
                    ),
                    replay_supported=replay,
                    computed_at=now,
                )
            )
    return out


def _tool_dependency(
    obs: list[Observation],
    project_id: str,
    trace_id: str,
    replay: bool,
    now: datetime,
) -> list[CausalEdge]:
    """Tool args quote a prior observation's output (≥ ``MIN_LEN`` chars)."""
    out: list[CausalEdge] = []
    for i, target in enumerate(obs):
        if target.type != "TOOL" and not target.tool_calls:
            continue
        for tc in target.tool_calls:
            args = (tc.arguments or "").strip()
            if len(args) < MIN_LEN:
                continue
            for prior in _lookback(obs, i):
                if prior.observation_id == target.observation_id:
                    continue
                prior_output = (prior.output or "").strip()
                if len(prior_output) < MIN_LEN:
                    continue
                if _shares_substring(prior_output, args, MIN_LEN):
                    out.append(
                        CausalEdge(
                            project_id=project_id,
                            trace_id=trace_id,
                            source_observation_id=prior.observation_id,
                            target_observation_id=target.observation_id,
                            edge_type="TOOL_DEPENDENCY",
                            confidence=_CONF_TOOL_DEPENDENCY,
                            method="STATIC",
                            evidence=(
                                f"tool {tc.name!r} arguments quote a "
                                f"≥{MIN_LEN}-char substring of an earlier "
                                f"observation's output"
                            ),
                            replay_supported=replay,
                            computed_at=now,
                        )
                    )
                    break  # one TOOL_DEPENDENCY edge per (source, target) pair
    return out


def _prompt_conditioning(
    obs: list[Observation],
    project_id: str,
    trace_id: str,
    replay: bool,
    now: datetime,
) -> list[CausalEdge]:
    """``GENERATION``'s input quotes a prior sibling's output."""
    out: list[CausalEdge] = []
    for i, target in enumerate(obs):
        if target.type != "GENERATION":
            continue
        target_input = (target.input or "").strip()
        if len(target_input) < MIN_LEN:
            continue
        for prior in _lookback(obs, i):
            if prior.parent_observation_id != target.parent_observation_id:
                continue
            if prior.observation_id == target.observation_id:
                continue
            prior_output = (prior.output or "").strip()
            if len(prior_output) < MIN_LEN:
                continue
            if _shares_substring(prior_output, target_input, MIN_LEN):
                out.append(
                    CausalEdge(
                        project_id=project_id,
                        trace_id=trace_id,
                        source_observation_id=prior.observation_id,
                        target_observation_id=target.observation_id,
                        edge_type="PROMPT_CONDITIONING",
                        confidence=_CONF_PROMPT_CONDITIONING,
                        method="STATIC",
                        evidence=(
                            "GENERATION input contains a sibling observation's "
                            "output verbatim"
                        ),
                        replay_supported=replay,
                        computed_at=now,
                    )
                )
                break
    return out


def _retrieval_to_generation(
    obs: list[Observation],
    project_id: str,
    trace_id: str,
    replay: bool,
    now: datetime,
) -> list[CausalEdge]:
    """Most recent ``RETRIEVAL`` whose output appears in the next ``GENERATION``."""
    out: list[CausalEdge] = []
    for i, target in enumerate(obs):
        if target.type != "GENERATION":
            continue
        target_input = (target.input or "").strip()
        if len(target_input) < MIN_LEN:
            continue
        for prior in _lookback(obs, i):
            if prior.type != "RETRIEVAL":
                continue
            prior_output = (prior.output or "").strip()
            if len(prior_output) < MIN_LEN:
                continue
            if _shares_substring(prior_output, target_input, MIN_LEN):
                out.append(
                    CausalEdge(
                        project_id=project_id,
                        trace_id=trace_id,
                        source_observation_id=prior.observation_id,
                        target_observation_id=target.observation_id,
                        edge_type="DATAFLOW",
                        confidence=_CONF_RETRIEVAL_TO_GEN,
                        method="STATIC",
                        evidence=(
                            "RETRIEVAL output appears in the next GENERATION's "
                            "input — likely RAG conditioning"
                        ),
                        replay_supported=replay,
                        computed_at=now,
                    )
                )
                break  # take the most recent retrieval only
    return out


# --------------------------------------------------------------------------- #


def _lookback(obs: list[Observation], i: int) -> list[Observation]:
    """Return up to ``MAX_LOOKBACK_SPANS`` observations preceding index ``i``."""
    start = max(0, i - MAX_LOOKBACK_SPANS)
    return list(reversed(obs[start:i]))


def _shares_substring(haystack: str, needle: str, min_len: int) -> bool:
    """Return True iff ``haystack`` and ``needle`` share a substring of length
    ``≥ min_len``.

    Uses a sliding-window approach: scan windows of ``needle`` and check
    membership in ``haystack``. Linear in ``len(needle)``; suitable for the
    body-size scales we offload at 64 KiB (§6.6).
    """
    if len(haystack) < min_len or len(needle) < min_len:
        return False
    # Quick fast-path: full needle in haystack.
    if needle in haystack:
        return True
    for start in range(len(needle) - min_len + 1):
        chunk = needle[start : start + min_len]
        if chunk in haystack:
            return True
    return False


# Expose tool_call helper for tests / Stage-2 wiring.
def extract_tool_call_inputs(o: Observation) -> list[ToolCall]:
    """Return ``o``'s tool calls in their declared order."""
    return list(o.tool_calls)


__all__ = [
    "MAX_LOOKBACK_SPANS",
    "MIN_LEN",
    "extract_tool_call_inputs",
    "stage1_static",
]
