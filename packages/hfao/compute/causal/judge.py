"""Stage 3 — LLM-judge causal attribution (SPEC §8.1).

Renders a compact view of the failing trace, asks the configured judge model
*"which agent and which step caused the failure, and why?"*, and returns
ranked :class:`CausalEdge` hypotheses with confidence.

**Critical UX rule (§8.1, Appendix C rule 2):** every edge emitted here
carries ``method = "LLM_JUDGE"`` and an explicit evidence string, so the UI
and MCP can label these as *hypotheses, not verdicts*. Per-edge
``replay_supported`` is set from the framework registry — Stage 3 never
fabricates replay support.

The judge backend is a :class:`Judge` protocol so:

  * **Production** uses ``AnthropicJudge`` → ``OpenAIJudge`` → ``HFJudge``
    based on ``HFAO_JUDGE_PROVIDER`` (Appendix A defaults: Q-1 resolution).
  * **AC tests** inject a ``DeterministicJudge`` so coverage doesn't require
    network or API keys.

Constructing a real provider's client is **lazy**: imports happen only on
first use, so installing one of the optional SDKs is not required for
non-judge code paths.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, cast

from hfao.compute.causal.replay_support import (
    is_replay_supported,
    replay_supported_for_observations,
)
from hfao.schema.causal import CausalEdge

if TYPE_CHECKING:
    from hfao.config import HFAOConfig
    from hfao.schema.events import Observation

# Default candidate set size (§8.1).
TOP_K = 5

JUDGE_SYSTEM_PROMPT = (
    "You are an HFAO causal-attribution judge. You are given a structured "
    "summary of a failing multi-agent trace. Identify which agent and which "
    "step most likely caused the failure, and explain why in one sentence. "
    "If the evidence is weak, say so by lowering the confidence. Reply ONLY "
    "with a JSON object of the form: "
    '{"hypotheses": [{"observation_id": "...", "confidence": 0.0..1.0, '
    '"reason": "..."}]} ranked by confidence descending, max 5 entries. '
    "These are hypotheses, not verdicts."
)


@dataclass(frozen=True)
class JudgeHypothesis:
    """One judge-attributed hypothesis before it becomes a ``CausalEdge``."""

    observation_id: str
    confidence: float
    reason: str


class Judge(Protocol):
    """Pluggable backend: render the trace + return ranked hypotheses."""

    model: str

    def attribute(
        self,
        observations: Sequence[Observation],
        candidates: Sequence[Observation],
    ) -> list[JudgeHypothesis]: ...


@dataclass
class DeterministicJudge:
    """Test/CI-friendly judge with no network.

    Picks the *latest error-status candidate* (falling back to the latest
    candidate) and assigns ``confidence = 0.6``. Reason quotes the
    observation's ``status_message`` when available. Deterministic given a
    fixed candidate list.
    """

    model: str = "deterministic-judge"

    def attribute(
        self,
        observations: Sequence[Observation],
        candidates: Sequence[Observation],
    ) -> list[JudgeHypothesis]:
        del observations
        if not candidates:
            return []
        ordered = sorted(candidates, key=lambda o: o.start_time, reverse=True)
        for o in ordered:
            if o.status == "error":
                return [
                    JudgeHypothesis(
                        observation_id=o.observation_id,
                        confidence=0.6,
                        reason=(
                            o.status_message
                            or f"latest error in {o.name!r} (no status_message)"
                        ),
                    )
                ]
        latest = ordered[0]
        return [
            JudgeHypothesis(
                observation_id=latest.observation_id,
                confidence=0.4,
                reason=f"no explicit error; latest activity at {latest.name!r}",
            )
        ]


def select_judge(config: HFAOConfig) -> Judge:
    """Resolve the configured judge per §16 Q-1 fallback chain.

    Order: configured provider → Anthropic → OpenAI → HF Inference Provider.
    Each constructor lazy-imports its SDK so we don't pay for it when unused.
    A ``DeterministicJudge`` is returned only when no provider can be
    constructed at all (e.g. all SDKs missing); the caller can detect this and
    surface a notice rather than silently shipping a stub in production.
    """
    chain: list[str] = [
        config.judge_provider,
        "anthropic",
        "openai",
        "hf",
    ]
    seen: set[str] = set()
    for provider in chain:
        if provider in seen:
            continue
        seen.add(provider)
        try:
            return _build_provider(provider, config)
        except (ImportError, RuntimeError):
            continue
    return DeterministicJudge()


def _build_provider(provider: str, config: HFAOConfig) -> Judge:
    if provider == "anthropic":
        return AnthropicJudge(model=config.judge_model)
    if provider == "openai":
        return OpenAIJudge(model=config.judge_model)
    if provider == "hf":
        return HFJudge(model=config.judge_model)
    raise RuntimeError(f"unknown judge provider: {provider!r}")


@dataclass
class AnthropicJudge:
    model: str

    _client: Any = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep

            self._client = anthropic.Anthropic()  # pyright: ignore[reportUnknownMemberType]
        return self._client  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]

    def attribute(
        self,
        observations: Sequence[Observation],
        candidates: Sequence[Observation],
    ) -> list[JudgeHypothesis]:
        prompt = _render_trace(observations, candidates)
        client = self._ensure_client()
        resp = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = _extract_anthropic_text(resp)
        return _parse_judge_json(text)


@dataclass
class OpenAIJudge:
    model: str

    _client: Any = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:
            import openai  # type: ignore[import-not-found]  # optional dep

            self._client = openai.OpenAI()  # pyright: ignore[reportUnknownMemberType]
        return self._client  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]

    def attribute(
        self,
        observations: Sequence[Observation],
        candidates: Sequence[Observation],
    ) -> list[JudgeHypothesis]:
        prompt = _render_trace(observations, candidates)
        client = self._ensure_client()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or "{}"
        return _parse_judge_json(text)


@dataclass
class HFJudge:
    """HF Inference Provider chat completion."""

    model: str

    _client: Any = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:
            from huggingface_hub import InferenceClient

            self._client = InferenceClient()
        return self._client

    def attribute(
        self,
        observations: Sequence[Observation],
        candidates: Sequence[Observation],
    ) -> list[JudgeHypothesis]:
        prompt = _render_trace(observations, candidates)
        client = self._ensure_client()
        resp = client.chat_completion(
            model=self.model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1024,
        )
        text = resp.choices[0].message.content or "{}"
        return _parse_judge_json(text)


# --------------------------------------------------------------------------- #


def stage3_judge(
    observations: list[Observation],
    hint_edges: list[CausalEdge],
    *,
    judge: Judge,
    k: int = TOP_K,
) -> list[CausalEdge]:
    """Run the LLM judge and return up to ``k`` ranked ``LLM_JUDGE`` edges.

    Candidate selection: the targets of the highest-confidence ``hint_edges``
    (deduped). If there are none, fall back to all observations with
    ``status == "error"``; if still empty, the last ``k`` observations.

    Every emitted edge:
      - has ``method = "LLM_JUDGE"`` and ``edge_type = "DECISIVE_ERROR"``;
      - quotes the judge's reason in ``evidence``;
      - sets ``replay_supported`` from the per-observation framework registry
        (not the trace-wide flag — Stage 2 selects per node);
      - sets ``judge_model`` to ``judge.model``;
      - links the failure target to itself (source = target = the candidate)
        — Stage 3 attributes blame to *the* observation; HANDOFF lineage that
        led to it is already on the Stage 1 edges.

    Confidence is clamped to ``[0.0, 1.0]``.
    """
    if not observations:
        return []
    candidates = _select_candidates(observations, hint_edges, k=k)
    if not candidates:
        return []
    hypotheses = judge.attribute(observations, candidates)
    by_id = {o.observation_id: o for o in observations}
    project_id = observations[0].project_id
    trace_id = observations[0].trace_id
    now = datetime.now(timezone.utc)
    trace_replay = replay_supported_for_observations(observations)
    out: list[CausalEdge] = []
    for hyp in hypotheses[:k]:
        target = by_id.get(hyp.observation_id)
        if target is None:
            # Judge hallucinated an observation id; skip silently — never
            # invent an edge.
            continue
        # Per-observation replay support; fall back to the trace-wide answer
        # when the judge picked a span without framework metadata.
        from hfao.compute.causal.replay_support import framework_of

        per_obs_replay = is_replay_supported(framework_of(target)) or trace_replay
        out.append(
            CausalEdge(
                project_id=project_id,
                trace_id=trace_id,
                source_observation_id=target.observation_id,
                target_observation_id=target.observation_id,
                edge_type="DECISIVE_ERROR",
                confidence=max(0.0, min(1.0, float(hyp.confidence))),
                method="LLM_JUDGE",
                evidence=hyp.reason,
                replay_supported=per_obs_replay,
                judge_model=judge.model,
                computed_at=now,
            )
        )
    out.sort(key=lambda e: e.confidence, reverse=True)
    return out


# --------------------------------------------------------------------------- #


def _select_candidates(
    observations: list[Observation],
    hint_edges: list[CausalEdge],
    *,
    k: int,
) -> list[Observation]:
    """Pick the top-k candidate observations for the judge to weigh."""
    by_id = {o.observation_id: o for o in observations}
    # Targets of hint edges, ranked by hint confidence.
    seen: set[str] = set()
    ranked: list[Observation] = []
    for edge in sorted(hint_edges, key=lambda e: e.confidence, reverse=True):
        oid = edge.target_observation_id
        if oid in seen:
            continue
        target = by_id.get(oid)
        if target is None:
            continue
        seen.add(oid)
        ranked.append(target)
        if len(ranked) >= k:
            return ranked
    # Pad with error observations not already in the list.
    for o in observations:
        if o.status == "error" and o.observation_id not in seen:
            seen.add(o.observation_id)
            ranked.append(o)
            if len(ranked) >= k:
                return ranked
    # Final fallback: the latest few observations by start_time.
    if not ranked:
        return sorted(observations, key=lambda o: o.start_time, reverse=True)[:k]
    return ranked


def _render_trace(
    observations: Sequence[Observation], candidates: Sequence[Observation]
) -> str:
    """Compact JSON view the judge prompt is built around."""
    candidate_ids = {o.observation_id for o in candidates}
    rendered_obs = [
        {
            "observation_id": o.observation_id,
            "parent": o.parent_observation_id,
            "agent": o.agent_id,
            "agent_role": o.agent_role,
            "type": o.type,
            "name": o.name,
            "status": o.status,
            "status_message": o.status_message,
            "model": o.model,
            "tool_calls": [t.name for t in o.tool_calls],
            "duration_ms": o.duration_ms,
        }
        for o in observations
    ]
    payload = {
        "candidates": sorted(candidate_ids),
        "observations": rendered_obs,
    }
    return json.dumps(payload, default=str)


def _extract_anthropic_text(response: Any) -> str:
    """Concatenate text blocks from an Anthropic ``Messages.create`` response."""
    pieces: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            pieces.append(text)
    return "".join(pieces) if pieces else "{}"


def _parse_judge_json(raw: str) -> list[JudgeHypothesis]:
    """Parse the judge JSON envelope; tolerate code fences and stray prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        # Strip leading 'json' marker if present.
        if text.lower().startswith("json"):
            text = text[len("json") :]
        text = text.strip()
    try:
        payload = cast("dict[str, Any]", json.loads(text))
    except json.JSONDecodeError:
        return []
    raw_hypotheses = cast("list[Any]", payload.get("hypotheses") or [])
    out: list[JudgeHypothesis] = []
    for entry in raw_hypotheses:
        if not isinstance(entry, dict):
            continue
        item = cast("dict[str, Any]", entry)
        oid = item.get("observation_id")
        conf = item.get("confidence")
        reason = item.get("reason") or ""
        if oid is None or conf is None:
            continue
        try:
            out.append(
                JudgeHypothesis(
                    observation_id=str(oid),
                    confidence=float(conf),
                    reason=str(reason),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "AnthropicJudge",
    "DeterministicJudge",
    "HFJudge",
    "JUDGE_SYSTEM_PROMPT",
    "Judge",
    "JudgeHypothesis",
    "OpenAIJudge",
    "TOP_K",
    "select_judge",
    "stage3_judge",
]
