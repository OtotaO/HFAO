"""OpenAI Agents SDK → HFAO tracing processor.

SPEC §12.2. The OpenAI Agents SDK ships its own in-process trace
tree (AgentSpan, FunctionSpan, GenerationSpan, HandoffSpan,
GuardrailSpan, MCPListToolsSpan, ResponseSpan, SpeechSpan, …). The
``openinference-instrumentation-openai-agents`` package translates a
subset of those into OTel spans; this module layers HFAO-specific
handling on top so:

- ``HandoffSpan`` → OTel span with ``openinference.span.kind=AGENT`` and
  ``handoff.target_agent_id`` attribute → canonical
  ``ObservationType.HANDOFF`` and ``handoff_target_agent_id`` (§4.1).
- ``GuardrailSpan`` → ``openinference.span.kind=GUARDRAIL`` →
  ``ObservationType.GUARDRAIL`` (§5.3).
- ``MCPListToolsSpan`` → ``openinference.span.kind=TOOL`` with
  ``tool.name="mcp.list_tools"`` so the tool-index aggregations still
  see it (§4.1 tool_call_names).

Usage (per §12.2):

    import hfao
    from agents import set_trace_processors
    from hfao.instrumentations import openai_agents_extra

    hfao.init(project="my-project")
    set_trace_processors([openai_agents_extra.HFAOTracingProcessor()])

Or rely on the convenience installer which is a no-op when the
``agents`` package is not importable:

    openai_agents_extra.install()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from hfao.compute.causal.counterfactual import ReplayOutcome
    from hfao.schema.events import Observation

_logger = logging.getLogger(__name__)
_TRACER_NAME = "hfao.instrumentations.openai_agents"

# agents.tracing span class name → OpenInference span.kind.
# Mapping is derived from the agents SDK's public span types; unknown
# classes fall through as "CHAIN" (→ canonical SPAN per §5.3).
_SPAN_KIND_BY_CLASS: dict[str, str] = {
    "AgentSpan": "AGENT",
    "AgentSpanData": "AGENT",
    "FunctionSpan": "TOOL",
    "FunctionSpanData": "TOOL",
    "GenerationSpan": "LLM",
    "GenerationSpanData": "LLM",
    "ResponseSpan": "LLM",
    "ResponseSpanData": "LLM",
    "HandoffSpan": "AGENT",
    "HandoffSpanData": "AGENT",
    "GuardrailSpan": "GUARDRAIL",
    "GuardrailSpanData": "GUARDRAIL",
    "MCPListToolsSpan": "TOOL",
    "MCPListToolsSpanData": "TOOL",
    "SpeechSpan": "LLM",
    "SpeechSpanData": "LLM",
    "SpeechGroupSpan": "CHAIN",
    "SpeechGroupSpanData": "CHAIN",
    "TranscriptionSpan": "LLM",
    "TranscriptionSpanData": "LLM",
}


class HFAOTracingProcessor:
    """OpenAI Agents SDK ``TracingProcessor`` that re-emits spans via OTel.

    The class duck-types the ``agents.tracing.TracingProcessor`` protocol;
    it does not import from ``agents`` at module-load time so it stays
    safely importable when the SDK is absent.
    """

    def __init__(self) -> None:
        self._tracer = trace.get_tracer(_TRACER_NAME)
        # agents span_id → (otel context-manager, otel span)
        self._active: dict[str, tuple[Any, trace.Span]] = {}

    # -- agents.tracing.TracingProcessor interface ---------------------------

    def on_trace_start(self, trace_obj: Any) -> None:  # noqa: ANN401 — SDK callback
        return

    def on_trace_end(self, trace_obj: Any) -> None:  # noqa: ANN401 — SDK callback
        return

    def on_span_start(self, span_obj: Any) -> None:  # noqa: ANN401 — SDK callback
        name, kind, attrs = _extract_span_fields(span_obj)
        cm = self._tracer.start_as_current_span(name=name, attributes=attrs)
        otel_span = cm.__enter__()
        otel_span.set_attribute("openinference.span.kind", kind)
        span_id = getattr(span_obj, "span_id", None) or str(id(span_obj))
        self._active[str(span_id)] = (cm, otel_span)

    def on_span_end(self, span_obj: Any) -> None:  # noqa: ANN401 — SDK callback
        span_id = str(getattr(span_obj, "span_id", None) or id(span_obj))
        entry = self._active.pop(span_id, None)
        if entry is None:
            return
        cm, otel_span = entry
        error = getattr(span_obj, "error", None)
        if error is not None:
            otel_span.set_status(Status(StatusCode.ERROR, str(error)))
            otel_span.set_attribute("hfao.agents.error", str(error))
        cm.__exit__(None, None, None)

    def shutdown(self) -> None:
        for cm, _otel_span in self._active.values():
            try:
                cm.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 — shutdown must not throw
                _logger.warning("HFAOTracingProcessor shutdown span close failed: %s", exc)
        self._active.clear()

    def force_flush(self) -> None:
        return


def install() -> bool:
    """Register :class:`HFAOTracingProcessor` via ``agents.set_trace_processors``.

    Returns ``True`` on success, ``False`` when the ``agents`` package is
    not importable. Safe to call unconditionally from user code.
    """
    try:
        from agents import set_trace_processors  # type: ignore[import-not-found]
    except ImportError:
        return False
    set_trace_processors([HFAOTracingProcessor()])
    return True


# -- helpers ----------------------------------------------------------------


def _extract_span_fields(span_obj: Any) -> tuple[str, str, dict[str, str]]:  # noqa: ANN401
    """Pull name, OpenInference kind, and attribute dict from an agents span.

    Tolerates both the v0.0.x (pre-GA) and GA shapes of the agents SDK:
    some releases expose attributes directly on the span, others wrap
    them in ``span.span_data``. We read ``span_data`` first, fall through
    to the span itself.
    """
    data = getattr(span_obj, "span_data", None) or span_obj
    class_names = (type(span_obj).__name__, type(data).__name__)
    kind = next(
        (_SPAN_KIND_BY_CLASS[name] for name in class_names if name in _SPAN_KIND_BY_CLASS),
        "CHAIN",
    )
    name = str(
        getattr(data, "name", None)
        or getattr(span_obj, "name", None)
        or class_names[-1]
    )
    attrs: dict[str, str] = {}
    for key, fn in _iter_attr_fields(kind):
        value = fn(data)
        if value is not None:
            attrs[key] = str(value)
    return name, kind, attrs


def _class_name(d: Any) -> object | None:  # noqa: ANN401 — SDK duck type
    return type(d).__name__


def _attr(name: str) -> Callable[[Any], object | None]:
    def _read(d: Any) -> object | None:  # noqa: ANN401 — SDK duck type
        return getattr(d, name, None)

    return _read


def _mcp_tool_count(d: Any) -> object | None:  # noqa: ANN401 — SDK duck type
    if not hasattr(d, "result"):
        return None
    result: Any = getattr(d, "result", None)
    if result is None:
        return 0
    try:
        return len(result)
    except TypeError:
        return None


def _iter_attr_fields(kind: str) -> Iterator[tuple[str, Callable[[Any], object | None]]]:
    """Yield ``(otel_attr_key, extractor)`` pairs for the given span kind.

    Extractors are defensive: missing fields become ``None`` and are
    dropped from the final attribute dict without raising.
    """
    yield ("agents.span.kind", _class_name)
    if kind == "AGENT":
        yield ("agent.name", _attr("name"))
        yield ("handoff.target_agent_id", _attr("target_agent"))
    elif kind == "TOOL":
        yield ("tool.name", _attr("name"))
        # MCPListToolsSpan exposes the discovered tool list; keep a count
        # so aggregations don't need to parse the payload.
        yield ("mcp.tools.count", _mcp_tool_count)
    elif kind == "LLM":
        yield ("llm.model_name", _attr("model"))
        yield ("llm.provider", _attr("provider"))
    elif kind == "GUARDRAIL":
        yield ("guardrail.name", _attr("name"))
        yield ("guardrail.triggered", _attr("triggered"))


class OpenAIAgentsReplayDriver:
    """Stage-2 replay driver for the OpenAI Agents SDK (SPEC §16 Q-20).

    Resumes via ``RunState.from_string(state_json) + Runner.run`` (the
    canonical pattern the SDK exposes for checkpointing). The serialised state
    lives on the failing observation's
    ``metadata['openai_agents.run_state']``; the agent constructor is
    caller-supplied via ``agent_factory``.

    The ``agents`` package is **not** imported here. The caller wires both
    the agent and the resume callable; the driver only orchestrates.
    """

    framework: str = "openai_agents"

    def __init__(
        self,
        *,
        resume: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        """``resume(state_json, perturb) -> result`` runs the agent. The driver
        treats a result with a ``final_output`` attribute / ``"final_output"``
        key as a successful flip."""
        self._resume = resume

    def can_replay(self, observation: Observation) -> bool:
        md = observation.metadata or {}
        return bool(md.get("openai_agents.run_state"))

    def replay(
        self, *, trace: Sequence[Observation], candidate: Observation
    ) -> ReplayOutcome:
        from hfao.compute.causal.counterfactual import ReplayOutcome

        if self._resume is None:
            return ReplayOutcome(
                framework=self.framework,
                observation_id=candidate.observation_id,
                flipped=False,
                new_status=candidate.status,
                evidence="OpenAIAgentsReplayDriver: no resume callable wired",
                driver_error="missing_resume",
            )
        state = (candidate.metadata or {}).get("openai_agents.run_state")
        if not state:
            return ReplayOutcome(
                framework=self.framework,
                observation_id=candidate.observation_id,
                flipped=False,
                new_status=candidate.status,
                evidence="OpenAIAgentsReplayDriver: candidate missing run_state",
                driver_error="missing_run_state",
            )
        try:
            result = self._resume(state, {})
        except Exception as exc:  # noqa: BLE001
            return ReplayOutcome(
                framework=self.framework,
                observation_id=candidate.observation_id,
                flipped=False,
                new_status=candidate.status,
                evidence=f"Agents resume raised: {exc!s}",
                driver_error=str(exc),
            )
        flipped = _agents_result_flipped(result)
        del trace
        return ReplayOutcome(
            framework=self.framework,
            observation_id=candidate.observation_id,
            flipped=flipped,
            new_status="ok" if flipped else "error",
            evidence=f"OpenAI Agents SDK resume flipped={flipped}",
        )


def _agents_result_flipped(result: Any) -> bool:
    """A successful Agents result has a populated ``final_output``."""
    from typing import cast as _cast

    if result is None:
        return False
    if isinstance(result, dict):
        fo: Any = _cast("dict[str, Any]", result).get("final_output")
        return fo is not None and fo != ""
    fo = getattr(result, "final_output", None)
    return fo is not None and fo != ""


def install_replay_driver(
    *, resume: Callable[[str, dict[str, Any]], Any] | None = None
) -> OpenAIAgentsReplayDriver:
    """Convenience: construct + register the OpenAI Agents replay driver."""
    from hfao.compute.causal.counterfactual import register_driver

    driver = OpenAIAgentsReplayDriver(resume=resume)
    register_driver(driver)
    return driver


__all__ = [
    "HFAOTracingProcessor",
    "OpenAIAgentsReplayDriver",
    "install",
    "install_replay_driver",
]
