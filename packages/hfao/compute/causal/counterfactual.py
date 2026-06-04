"""Stage 2 — counterfactual replay (SPEC §8.1, §16 Q-20).

Given a failing trace and a set of candidate observations from Stage 1, try
to re-run the agent from each candidate with a minimally perturbed input.
If the failure flips to success, mark that candidate as a verified decisive
error (``method = COUNTERFACTUAL_REPLAY``).

Per the §16 Q-20 caveat, the replay surface is **driver-pluggable** so:

  * AC tests inject a deterministic in-process stub (no live LLM call);
  * production wires a per-framework driver via :func:`register_driver`
    once the framework's instrumentor calls
    ``hfao.compute.causal.counterfactual.register_driver(<driver>)`` at
    `hfao.init()` time.

Drivers come in three flavours per §12.2:

  * **LangGraph** — checkpointer ``thread_id`` + ``config`` resume.
  * **OpenAI Agents SDK** — ``RunState.from_string`` + ``Runner.run``.
  * **Claude Agent SDK** — ``resume_from``.

The first three ship as thin shims under
:mod:`hfao.instrumentations.langgraph_extra` /
:mod:`hfao.instrumentations.openai_agents_extra` /
:mod:`hfao.instrumentations.claude_agent_extra` so the dependency edge
stays "instrumentations import compute.causal", not the reverse.

The pipeline (:mod:`hfao.compute.causal.pipeline`) calls
:func:`stage2_counterfactual` once Stage 1 + Stage 3 finish. Failures from
any driver are non-fatal — Stage 2 is a *bonus* over Stage 3, never a
prerequisite. A failed replay leaves the Stage 3 hypothesis unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from hfao.compute.causal.replay_support import framework_of, is_replay_supported
from hfao.schema.causal import CausalEdge

if TYPE_CHECKING:
    from hfao.schema.events import Observation


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Replay outcome
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplayOutcome:
    """The result of one driver invocation."""

    framework: str
    observation_id: str
    flipped: bool
    new_status: str
    evidence: str
    driver_error: str | None = None


# --------------------------------------------------------------------------- #
# Driver protocol + registry
# --------------------------------------------------------------------------- #


class ReplayDriver(Protocol):
    """A framework-specific replay backend.

    ``framework`` is the lowercase key from
    :mod:`hfao.compute.causal.replay_support` (e.g. ``"langgraph"``,
    ``"openai_agents"``, ``"claude_agent_sdk"``).

    ``can_replay`` returns ``True`` iff the candidate observation carries
    enough metadata for this driver to resume (e.g. a checkpointer
    ``thread_id``). It must be fast — the orchestrator calls it on every
    candidate.

    ``replay`` runs the resume + returns an :class:`ReplayOutcome`. Drivers
    are responsible for their own per-framework setup; the orchestrator only
    knows the protocol.
    """

    framework: str

    def can_replay(self, observation: Observation) -> bool: ...

    def replay(
        self, *, trace: Sequence[Observation], candidate: Observation
    ) -> ReplayOutcome: ...


_DRIVERS: dict[str, ReplayDriver] = {}


def register_driver(driver: ReplayDriver) -> None:
    """Register a replay driver under its declared ``framework`` key.

    Idempotent: re-registering the same driver replaces the previous one.
    Called from each framework's ``hfao.instrumentations.*_extra`` module
    at `hfao.init()` time when the optional dep is installed.
    """
    _DRIVERS[driver.framework.lower()] = driver


def get_driver(framework: str | None) -> ReplayDriver | None:
    """Return the registered driver for ``framework`` or ``None`` if absent."""
    if not framework:
        return None
    return _DRIVERS.get(framework.lower())


def list_drivers() -> list[str]:
    """Return the registered driver framework keys (sorted, stable)."""
    return sorted(_DRIVERS.keys())


def clear_drivers() -> None:
    """Clear the registry. AC fixture helper; do not call in production."""
    _DRIVERS.clear()


# --------------------------------------------------------------------------- #
# Candidate ranking
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """A Stage-1 + Stage-3 hypothesis ranked for replay."""

    observation: Observation
    hint_confidence: float
    edge_id: tuple[str, str, str]  # (source, target, method)


def rank_for_replay(
    observations: Sequence[Observation],
    hint_edges: Sequence[CausalEdge],
    *,
    max_candidates: int = 5,
) -> list[Candidate]:
    """Order observations by their hint-edge confidence, keeping only
    replay-supported framework targets.

    Hints are deduped on target observation; the top ``max_candidates`` are
    returned. Observations with no associated driver are skipped — Stage 2
    declines to attempt a replay it cannot actually perform.
    """
    by_id = {o.observation_id: o for o in observations}
    seen: set[str] = set()
    out: list[Candidate] = []
    for edge in sorted(hint_edges, key=lambda e: e.confidence, reverse=True):
        if edge.target_observation_id in seen:
            continue
        target = by_id.get(edge.target_observation_id)
        if target is None:
            continue
        framework = framework_of(target)
        if not is_replay_supported(framework):
            continue
        if get_driver(framework) is None:
            continue
        seen.add(edge.target_observation_id)
        out.append(
            Candidate(
                observation=target,
                hint_confidence=edge.confidence,
                edge_id=(
                    edge.source_observation_id,
                    edge.target_observation_id,
                    edge.method,
                ),
            )
        )
        if len(out) >= max_candidates:
            break
    return out


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def _empty_edges() -> list[CausalEdge]:
    return []


def _empty_errors() -> list[str]:
    return []


@dataclass(frozen=True)
class Stage2Result:
    """Aggregated outcome of one Stage-2 pass."""

    edges: list[CausalEdge] = field(default_factory=_empty_edges)
    candidates_evaluated: int = 0
    candidates_skipped_unsupported: int = 0
    driver_errors: list[str] = field(default_factory=_empty_errors)


def stage2_counterfactual(
    observations: list[Observation],
    hint_edges: list[CausalEdge],
    *,
    max_candidates: int = 5,
    project_id: str | None = None,
    trace_id: str | None = None,
) -> Stage2Result:
    """Replay each ranked candidate; emit a DECISIVE_ERROR edge per flip.

    Returns ``Stage2Result(edges=[], ...)`` when no candidates qualify; the
    pipeline merges these edges with the Stage-1 + Stage-3 set before persist.
    The base confidence for replay-verified edges is **0.97** — counterfactual
    evidence is the strongest signal HFAO can produce, and the UI surfaces
    this with ``method=COUNTERFACTUAL_REPLAY`` so users see how it was reached.
    """
    if not observations:
        return Stage2Result()
    candidates = rank_for_replay(
        observations, hint_edges, max_candidates=max_candidates
    )
    pid = project_id or observations[0].project_id
    tid = trace_id or observations[0].trace_id
    now = datetime.now(timezone.utc)
    edges: list[CausalEdge] = []
    skipped = 0
    errors: list[str] = []

    if not candidates:
        # All hint targets either lack a driver or aren't replay-supported.
        return Stage2Result(
            edges=[],
            candidates_evaluated=0,
            candidates_skipped_unsupported=len(observations),
            driver_errors=[],
        )

    for candidate in candidates:
        framework = framework_of(candidate.observation)
        driver = get_driver(framework)
        # rank_for_replay already filtered these, but guard against races.
        if driver is None:
            skipped += 1
            continue
        if not driver.can_replay(candidate.observation):
            skipped += 1
            continue
        try:
            outcome = driver.replay(
                trace=observations, candidate=candidate.observation
            )
        except Exception as exc:  # noqa: BLE001 — Stage 2 is best-effort
            log.exception(
                "replay driver %r failed for observation %s",
                framework,
                candidate.observation.observation_id,
            )
            errors.append(
                f"{framework}:{candidate.observation.observation_id}: {exc!s}"
            )
            continue
        if outcome.driver_error:
            errors.append(
                f"{framework}:{candidate.observation.observation_id}: "
                f"{outcome.driver_error}"
            )
        if outcome.flipped:
            edges.append(
                CausalEdge(
                    project_id=pid,
                    trace_id=tid,
                    source_observation_id=candidate.observation.observation_id,
                    target_observation_id=candidate.observation.observation_id,
                    edge_type="DECISIVE_ERROR",
                    confidence=0.97,
                    method="COUNTERFACTUAL_REPLAY",
                    evidence=(
                        f"{framework} replay flipped failure to "
                        f"{outcome.new_status!r}: {outcome.evidence}"
                    ),
                    replay_supported=True,
                    computed_at=now,
                )
            )

    return Stage2Result(
        edges=edges,
        candidates_evaluated=len(candidates),
        candidates_skipped_unsupported=skipped,
        driver_errors=errors,
    )


# --------------------------------------------------------------------------- #
# Test-friendly built-in: a deterministic in-process driver
# --------------------------------------------------------------------------- #


def _empty_flips() -> dict[str, bool]:
    return {}


def _empty_str_set() -> set[str]:
    return set()


@dataclass
class DeterministicReplayDriver:
    """No-network driver for AC tests + offline use.

    Accepts a ``flips`` map keyed by ``observation_id`` whose values are
    ``True`` when replay should report a status flip. Defaults to ``False``
    for any observation not listed.

    AC §8.5 line ``test_replay_supported_false_for_crewai`` is covered at the
    registry level (no driver registered); the AC test for Stage 2 itself
    uses this stub against a synthetic LangGraph-flagged observation.
    """

    framework: str = "langgraph"
    flips: dict[str, bool] = field(default_factory=_empty_flips)
    raises_for: set[str] = field(default_factory=_empty_str_set)

    def can_replay(self, observation: Observation) -> bool:
        # Same threshold the real LangGraph driver uses: requires a
        # checkpointer thread id in metadata.
        if not observation.metadata:
            return False
        return bool(observation.metadata.get("langgraph.thread_id"))

    def replay(
        self, *, trace: Sequence[Observation], candidate: Observation
    ) -> ReplayOutcome:
        del trace
        if candidate.observation_id in self.raises_for:
            raise RuntimeError(
                f"deterministic-driver raise for {candidate.observation_id}"
            )
        flip = self.flips.get(candidate.observation_id, False)
        return ReplayOutcome(
            framework=self.framework,
            observation_id=candidate.observation_id,
            flipped=flip,
            new_status="ok" if flip else "error",
            evidence=(
                "stub replay: simulated re-run with checkpoint-resume "
                "perturbation"
            ),
        )


__all__ = [
    "Candidate",
    "DeterministicReplayDriver",
    "ReplayDriver",
    "ReplayOutcome",
    "Stage2Result",
    "clear_drivers",
    "get_driver",
    "list_drivers",
    "rank_for_replay",
    "register_driver",
    "stage2_counterfactual",
]
