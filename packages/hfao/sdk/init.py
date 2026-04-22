"""HFAO SDK entry point.

SPEC §12.1. ``hfao.init()`` bootstraps an OpenTelemetry ``TracerProvider``
with an OTLP/HTTP exporter pointed at ``HFAO_BASE_URL/v1/traces``,
auto-instruments any installed OpenInference packages, and (fallback)
patches the ``mcp`` SDK to inject W3C ``traceparent`` into ``_meta`` per §5.4.

The call is idempotent: a second invocation returns the first
``HFAOContext`` without rebuilding the provider. ``reset_for_testing``
clears the singleton for acceptance tests.
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import TYPE_CHECKING

import msgspec
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from hfao.config import HFAOConfig

if TYPE_CHECKING:
    from hfao.sdk.context import HFAOContext

_logger = logging.getLogger(__name__)

# (module_path, class_name) pairs for OpenInference auto-instrumentation.
# SPEC §12.2: best-effort import. Missing packages are silently skipped.
_AUTO_INSTRUMENT_MODULES: tuple[tuple[str, str], ...] = (
    ("openinference.instrumentation.openai", "OpenAIInstrumentor"),
    ("openinference.instrumentation.anthropic", "AnthropicInstrumentor"),
    ("openinference.instrumentation.mistralai", "MistralAIInstrumentor"),
    ("openinference.instrumentation.groq", "GroqInstrumentor"),
    ("openinference.instrumentation.bedrock", "BedrockInstrumentor"),
    ("openinference.instrumentation.vertexai", "VertexAIInstrumentor"),
    ("openinference.instrumentation.google_genai", "GoogleGenAIInstrumentor"),
    ("openinference.instrumentation.langchain", "LangChainInstrumentor"),
    ("openinference.instrumentation.openai_agents", "OpenAIAgentsInstrumentor"),
    ("openinference.instrumentation.claude_agent_sdk", "ClaudeAgentSDKInstrumentor"),
    ("openinference.instrumentation.smolagents", "SmolagentsInstrumentor"),
    ("openinference.instrumentation.mcp", "MCPInstrumentor"),
)

_state_lock = threading.Lock()
_initialized: HFAOContext | None = None


def init(
    *,
    project: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    environment: str | None = None,
    auto_instrument: bool = True,
    patch_mcp: bool = True,
    tracer_provider: TracerProvider | None = None,
    span_processor: SpanProcessor | None = None,
    config: HFAOConfig | None = None,
) -> HFAOContext:
    """Bootstrap HFAO instrumentation.

    SPEC §12.1. Steps:

    1. Resolve ``HFAOConfig`` from env (``HFAO_BASE_URL`` / ``HFAO_API_KEY`` /
       ``HFAO_PROJECT``) with kwarg overrides.
    2. Build an OTel ``TracerProvider`` with an OTLP/HTTP exporter pointed at
       ``{base_url}/v1/traces`` and an ``Authorization: Bearer`` header when
       ``api_key`` is set.
    3. Auto-instrument installed OpenInference packages (best-effort).
    4. Patch MCP ``_meta`` when ``mcp`` is importable and the OpenInference
       MCP instrumentor is not installed (§5.4 fallback).

    Idempotent: a second call returns the previously installed ``HFAOContext``.
    Pass ``tracer_provider`` to bring your own provider (tests use an
    ``InMemorySpanExporter``); ``span_processor`` adds an extra processor
    alongside the OTLP exporter (useful for in-process capture).
    """
    from hfao.sdk.context import HFAOContext

    global _initialized
    with _state_lock:
        if _initialized is not None:
            return _initialized

        cfg = config if config is not None else HFAOConfig.from_env()
        overrides: dict[str, object] = {}
        if project is not None:
            overrides["project"] = project
        if base_url is not None:
            overrides["base_url"] = base_url
        if api_key is not None:
            overrides["api_key"] = api_key
        if environment is not None:
            overrides["environment"] = environment
        if overrides:
            cfg = msgspec.structs.replace(cfg, **overrides)

        provider = tracer_provider if tracer_provider is not None else _build_provider(cfg)
        from hfao.sdk.context import HFAOBaggageSpanProcessor

        provider.add_span_processor(HFAOBaggageSpanProcessor())
        if span_processor is not None:
            provider.add_span_processor(span_processor)
        trace.set_tracer_provider(provider)

        instrumented: tuple[str, ...] = ()
        if auto_instrument:
            instrumented = tuple(_auto_instrument(provider))

        mcp_patched = False
        if patch_mcp:
            mcp_patched = _patch_mcp_meta()

        ctx = HFAOContext(
            config=cfg,
            tracer_provider=provider,
            instrumentors=instrumented,
            mcp_patched=mcp_patched,
        )
        _initialized = ctx
        return ctx


def current_context() -> HFAOContext | None:
    """Return the singleton ``HFAOContext`` installed by :func:`init`, if any."""
    return _initialized


def require_context() -> HFAOContext:
    """Return the singleton ``HFAOContext`` or raise ``RuntimeError``."""
    if _initialized is None:
        raise RuntimeError(
            "hfao.init() has not been called; SDK surface is not bootstrapped"
        )
    return _initialized


def reset_for_testing() -> None:
    """Clear the ``HFAOContext`` singleton. Test-only."""
    global _initialized
    with _state_lock:
        if _initialized is not None:
            _uninstrument(_initialized.instrumentors)
        _initialized = None


def _build_provider(cfg: HFAOConfig) -> TracerProvider:
    endpoint = f"{cfg.base_url.rstrip('/')}/v1/traces"
    headers: dict[str, str] = {}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    resource = Resource.create(
        {
            "service.name": cfg.project,
            "hfao.project_id": cfg.project,
            "deployment.environment": cfg.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or None)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def _auto_instrument(provider: TracerProvider) -> list[str]:
    instrumented: list[str] = []
    for mod_path, cls_name in _AUTO_INSTRUMENT_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        klass = getattr(mod, cls_name, None)
        if klass is None:
            continue
        try:
            klass().instrument(tracer_provider=provider)
        except Exception as exc:  # noqa: BLE001 — third-party instrumentors raise anything
            _logger.warning("hfao auto-instrument skipped %s.%s: %s", mod_path, cls_name, exc)
            continue
        instrumented.append(f"{mod_path}.{cls_name}")
    return instrumented


def _uninstrument(instrumentors: tuple[str, ...]) -> None:
    for ref in instrumentors:
        mod_path, _, cls_name = ref.rpartition(".")
        try:
            mod = importlib.import_module(mod_path)
            klass = getattr(mod, cls_name)
            klass().uninstrument()
        except Exception:  # noqa: BLE001 — best-effort teardown
            continue


def _patch_mcp_meta() -> bool:
    """Apply the HFAO MCP ``_meta`` fallback patch (§5.4, §12.1 rule 4).

    Preferred path: ``openinference-instrumentation-mcp`` handles both client
    inject and server extract. When that package is installed this function
    is a no-op and returns ``False`` (not patched by HFAO).

    Fallback path: §16 Q-16 defers the HFAO-owned fallback to Week 6, when
    the §9 MCP server lands and supplies the server-side request handling
    that ``Server.tool`` extraction relies on. Until then this function is
    a documented stub that logs a one-shot note.
    """
    try:
        importlib.import_module("openinference.instrumentation.mcp")
        return False
    except ImportError:
        pass
    try:
        importlib.import_module("mcp")
    except ImportError:
        return False
    _logger.info(
        "hfao.init(): install openinference-instrumentation-mcp for MCP "
        "context propagation; HFAO fallback patch deferred to Week 6 (see §16 Q-16)"
    )
    return False


__all__ = ["init", "current_context", "require_context", "reset_for_testing"]
