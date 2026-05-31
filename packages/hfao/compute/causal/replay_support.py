"""Per-framework counterfactual-replay support registry (SPEC §8.1, §12.2).

The `replay_supported` flag on every :class:`hfao.schema.causal.CausalEdge` is
set from this registry so the UI / MCP **never** claim replay-supported for a
framework where Stage 2 will not actually work (an AC §12 case).

A framework is treated as replay-supported only when SPEC §12.2 names a real
driver (LangGraph checkpointer, OpenAI Agents SDK RunState, Claude Agent SDK
``resume_from``). Everything else — CrewAI, smolagents, AutoGen, raw LLM SDK
spans, generic OpenInference / OTel GenAI without a framework tag — is
``False``.

We infer the framework from observation metadata that the instrumentations
emit (``framework`` / ``openinference.framework`` / span name prefixes). When
no framework tag is present, we default to ``False`` — honest by default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hfao.schema.events import Observation

REPLAY_SUPPORTED_FRAMEWORKS: frozenset[str] = frozenset(
    {
        "langgraph",
        "openai-agents",
        "openai_agents",
        "claude-agent-sdk",
        "claude_agent_sdk",
    }
)

REPLAY_UNSUPPORTED_FRAMEWORKS: frozenset[str] = frozenset(
    {
        "crewai",
        "smolagents",
        "transformers_agents",
        "autogen",
        "ag2",
        "dspy",
        "llama_index",
        "haystack",
        "pydantic_ai",
        "google_adk",
        "aws_strands",
        "litellm",
    }
)


def _framework_for(observation: Observation) -> str | None:
    """Best-effort framework identification from observation metadata."""
    md = observation.metadata or {}
    for key in ("framework", "openinference.framework", "hfao.framework"):
        if md.get(key):
            return md[key].strip().lower()
    # Fall back to span name prefixes the instrumentations emit, e.g.
    # ``crewai.task``, ``langgraph.node``, ``openai-agents.run``.
    name = (observation.name or "").lower()
    if "." in name:
        prefix = name.split(".", 1)[0]
        if prefix in REPLAY_SUPPORTED_FRAMEWORKS or prefix in REPLAY_UNSUPPORTED_FRAMEWORKS:
            return prefix
    return None


def is_replay_supported(framework: str | None) -> bool:
    """Return ``True`` iff ``framework`` is in the replay-supported registry."""
    if framework is None:
        return False
    return framework.strip().lower() in REPLAY_SUPPORTED_FRAMEWORKS


def replay_supported_for_observations(observations: list[Observation]) -> bool:
    """Return True iff *any* observation in the trace is from a replay-supported
    framework.

    A multi-framework trace where at least one agent is LangGraph (say) can
    still benefit from counterfactual replay at that node; we surface the flag
    optimistically as long as one supported framework is present. The Stage 2
    runner is responsible for selecting only replay-compatible candidates.

    Returns ``False`` when no framework metadata is present anywhere in the
    trace (honest default).
    """
    for o in observations:
        fw = _framework_for(o)
        if fw is not None and fw in REPLAY_SUPPORTED_FRAMEWORKS:
            return True
    return False


def framework_of(observation: Observation) -> str | None:
    """Public alias of the framework resolver (used by Stage 3 evidence text)."""
    return _framework_for(observation)


__all__ = [
    "REPLAY_SUPPORTED_FRAMEWORKS",
    "REPLAY_UNSUPPORTED_FRAMEWORKS",
    "framework_of",
    "is_replay_supported",
    "replay_supported_for_observations",
]
