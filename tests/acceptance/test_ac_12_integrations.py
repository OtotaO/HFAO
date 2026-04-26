"""AC §12 — framework integration acceptance tests.

SPEC §12.4, narrowed to Q-12's Tier 1 scope (LangGraph, OpenAI Agents
SDK, Claude Agent SDK, smolagents) plus auto-OpenInference coverage
for raw LLM SDKs. Tier 2 frameworks (crewai, litellm, mcp-as-
instrumentation, pydantic ai, adk, strands, dspy, llamaindex,
haystack, autogen) are deferred to the Week 8 shared harness per
Q-12 and are intentionally not parametrized here.

Per §12.4 the five baseline tests are:

- test_framework_quickstart_produces_canonical_trace
- test_hfao_init_idempotent
- test_hfao_session_propagates_to_session_id
- test_hfao_prompt_decorates_generation_span
- test_replay_supported_flag_correct_per_framework

These exercise the SDK end-to-end without requiring the real framework
packages to be installed; each fixture simulates the framework's span
emission shape so the HFAO extras (baggage propagation, handoff/
guardrail mapping, hook callbacks, code-action spans) are validated
against a deterministic input.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import hfao
import pytest
from hfao.instrumentations import (
    claude_agent_extra,
    langgraph_extra,
    openai_agents_extra,
    transformers_agents_extra,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Tier 1 frameworks under v1.0.0 AC per Q-12.
TIER1_FRAMEWORKS = ("langgraph", "openai_agents", "claude_agent_sdk", "smolagents")
# Auto-OpenInference LLM SDKs covered by hfao.init() per §12.2.
AUTO_LLM_SDKS = (
    "openai",
    "anthropic",
    "mistral",
    "groq",
    "bedrock",
    "vertex",
    "google_genai",
)


# ---------- fixtures -------------------------------------------------------
#
# OpenTelemetry enforces one global TracerProvider per process. We install
# it once at module scope and only reset the HFAO singleton + exporter
# between tests so every test still starts with a clean slate.


_PROVIDER = TracerProvider()
_EXPORTER = InMemorySpanExporter()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    hfao.reset_for_testing()
    _EXPORTER.clear()
    hfao.init(
        project="ac12",
        tracer_provider=_PROVIDER,
        auto_instrument=False,
        patch_mcp=False,
    )
    yield _EXPORTER
    hfao.reset_for_testing()
    _EXPORTER.clear()


def _finished(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(exporter.get_finished_spans())


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


# ---------- core SDK tests -------------------------------------------------


def test_hfao_init_idempotent(exporter: InMemorySpanExporter) -> None:
    first = hfao.current_context()
    second = hfao.init(project="other", auto_instrument=False)
    assert first is second, "hfao.init() must return the installed HFAOContext on re-entry"


def test_hfao_session_propagates_to_session_id(exporter: InMemorySpanExporter) -> None:
    with hfao.session(session_id="sess-7", user_id="alice"):
        tracer = hfao.current_context().tracer_provider.get_tracer("test")
        with tracer.start_as_current_span("child.llm"):
            pass

    finished = _finished(exporter)
    # Both the `hfao.session` root and the `child.llm` descendant must
    # carry session.id / user.id so the normalizer lifts session_id on
    # every observation, not just the root.
    names = {s.name for s in finished}
    assert {"hfao.session", "child.llm"}.issubset(names)
    for span in finished:
        attrs = _attrs(span)
        assert attrs.get("session.id") == "sess-7"
        assert attrs.get("user.id") == "alice"
        assert attrs.get("gen_ai.conversation.id") == "sess-7"


def test_hfao_prompt_decorates_generation_span(exporter: InMemorySpanExporter) -> None:
    tracer = hfao.current_context().tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("llm.generate"):
        hfao.prompt("summarise", version=3, label="production")

    span = _finished(exporter)[0]
    attrs = _attrs(span)
    assert attrs["hfao.prompt.name"] == "summarise"
    assert attrs["hfao.prompt.version"] == 3
    assert attrs["hfao.prompt.label"] == "production"


def test_observe_decorator_sync_and_async(exporter: InMemorySpanExporter) -> None:
    @hfao.observe(type="TOOL")
    def lookup(x: int) -> int:
        return x + 1

    @hfao.observe(name="async_tool", type="TOOL")
    async def async_lookup(x: int) -> int:
        return x * 2

    assert lookup(3) == 4
    assert asyncio.run(async_lookup(3)) == 6

    finished = _finished(exporter)
    names = {s.name for s in finished}
    assert any(name.endswith("lookup") for name in names)
    assert "async_tool" in names
    for span in finished:
        attrs = _attrs(span)
        assert attrs["openinference.span.kind"] == "TOOL"
        assert "input.value" in attrs
        assert "output.value" in attrs


def test_score_emits_genai_evaluation_event(exporter: InMemorySpanExporter) -> None:
    tracer = hfao.current_context().tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("llm.generate"):
        hfao.score("quality", value=0.82, source="LLM_JUDGE", judge_model="haiku-4-5")

    span = _finished(exporter)[0]
    [event] = span.events
    assert event.name == "gen_ai.evaluation.result"
    attrs = dict(event.attributes or {})
    assert attrs["gen_ai.evaluation.name"] == "quality"
    assert attrs["gen_ai.evaluation.score.value"] == pytest.approx(0.82)
    assert attrs["hfao.score.source"] == "LLM_JUDGE"
    assert attrs["hfao.score.judge_model"] == "haiku-4-5"


# ---------- per-framework tests --------------------------------------------


@pytest.mark.parametrize("framework", TIER1_FRAMEWORKS)
def test_framework_quickstart_produces_canonical_trace(
    exporter: InMemorySpanExporter, framework: str
) -> None:
    """Each Tier 1 framework's extra produces canonical HFAO attributes."""
    provider = hfao.current_context().tracer_provider
    if framework == "langgraph":
        langgraph_extra.install(provider)
        with langgraph_extra.using_thread("thread-123"):
            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("langchain.chain"):
                pass
        attrs = _attrs(_finished(exporter)[0])
        assert attrs["session.id"] == "thread-123"
        assert attrs["gen_ai.conversation.id"] == "thread-123"

    elif framework == "openai_agents":
        processor = openai_agents_extra.HFAOTracingProcessor()
        handoff = _FakeHandoffSpan()
        processor.on_span_start(handoff)
        processor.on_span_end(handoff)
        attrs = _attrs(_finished(exporter)[0])
        assert attrs["openinference.span.kind"] == "AGENT"
        assert attrs["handoff.target_agent_id"] == "billing-agent"

    elif framework == "claude_agent_sdk":
        hooks = claude_agent_extra.build_hooks()
        assert set(hooks) >= {"PreToolUse", "PostToolUse", "SessionStart"}
        # The pre-tool-use hook records an event on the active span.
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("claude.turn"):
            pre_cb = _unwrap_hook(hooks["PreToolUse"][0])
            asyncio.run(
                pre_cb(
                    {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                    "tool_123",
                    None,
                )
            )
        [event] = _finished(exporter)[0].events
        assert event.name == "claude_agent.pre_tool_use"
        event_attrs = dict(event.attributes or {})
        assert event_attrs["tool.name"] == "Bash"
        assert event_attrs["tool.call_id"] == "tool_123"

    elif framework == "smolagents":
        with transformers_agents_extra.using_code_action("1 + 1", agent_name="code-agent"):
            pass
        attrs = _attrs(_finished(exporter)[0])
        assert attrs["openinference.span.kind"] == "TOOL"
        assert attrs["tool.name"] == "python_interpreter"
        assert attrs["input.value"] == "1 + 1"
        assert attrs["hfao.replay_supported"] is False


def test_replay_supported_flag_correct_per_framework(
    exporter: InMemorySpanExporter,
) -> None:
    """§12.2 matrix: only Claude Agent SDK + LangGraph support replay in v1.

    The flag is carried on spans produced by the extras; the normalizer
    (SPEC §5.3) merges it into Observation.metadata downstream. smolagents
    is the one Tier 1 framework explicitly marked replay-unsupported.
    """
    with transformers_agents_extra.using_code_action("print('hi')"):
        pass
    attrs = _attrs(_finished(exporter)[0])
    assert attrs["hfao.replay_supported"] is False

    # mark_replay_unsupported must also work when called on a user span.
    tracer = hfao.current_context().tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("user.span"):
        transformers_agents_extra.mark_replay_unsupported()
    user_attrs = _attrs(_finished(exporter)[-1])
    assert user_attrs["hfao.replay_supported"] is False


@pytest.mark.parametrize("sdk", AUTO_LLM_SDKS)
def test_auto_openinference_llm_sdks_registered(sdk: str) -> None:
    """Auto-instrument table covers every LLM SDK named in §12.2.

    We don't install the OpenInference packages in test; we just assert
    the module-lookup table (`_AUTO_INSTRUMENT_MODULES`) carries the
    expected ``(module_path, class_name)`` entry so ``hfao.init()`` will
    pick the SDK up the moment its instrumentor is installed.
    """
    from hfao.sdk.init import _AUTO_INSTRUMENT_MODULES

    expected_module = {
        "openai": "openinference.instrumentation.openai",
        "anthropic": "openinference.instrumentation.anthropic",
        "mistral": "openinference.instrumentation.mistralai",
        "groq": "openinference.instrumentation.groq",
        "bedrock": "openinference.instrumentation.bedrock",
        "vertex": "openinference.instrumentation.vertexai",
        "google_genai": "openinference.instrumentation.google_genai",
    }[sdk]
    assert any(entry[0] == expected_module for entry in _AUTO_INSTRUMENT_MODULES), (
        f"{sdk!r} missing from hfao.sdk.init._AUTO_INSTRUMENT_MODULES"
    )


# ---------- helpers --------------------------------------------------------


@dataclass
class _HandoffSpanData:
    name: str = "handoff"
    target_agent: str = "billing-agent"


@dataclass
class _FakeHandoffSpan:
    """Mimics the GA ``agents.tracing.HandoffSpan`` shape."""

    span_id: str = "1"
    span_data: _HandoffSpanData = None  # type: ignore[assignment]
    error: object = None

    def __post_init__(self) -> None:
        if self.span_data is None:
            self.span_data = _HandoffSpanData()


# Override the class name so our _SPAN_KIND_BY_CLASS lookup hits "HandoffSpan".
_FakeHandoffSpan.__name__ = "HandoffSpan"


def _unwrap_hook(entry: Any) -> Any:  # noqa: ANN401 — SDK duck type
    """Extract the callable from either a bare callback or a HookMatcher."""
    hooks = getattr(entry, "hooks", None)
    if hooks is None:
        return entry
    return hooks[0]
