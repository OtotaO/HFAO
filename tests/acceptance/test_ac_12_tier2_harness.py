"""AC §12 Tier-2 instrumentation harness (SPEC §16 Q-12).

Per the Q-12 resolution, Tier-2 frameworks (CrewAI, AutoGen, DSPy, LlamaIndex,
Haystack, Pydantic AI, Google ADK, AWS Strands, LiteLLM, MCP-as-instrumentation)
do **not** get individual AC tests in :mod:`test_ac_12_integrations`. Instead,
they share this generic harness: any community-contributed instrumentor that
emits canonical-shape OTLP spans must round-trip through the HFAO normalizer
into a valid :class:`Observation` and persist + read back unchanged.

The harness is parameterized over a small **catalog** of synthetic spans, one
per Tier-2 framework, demonstrating the contract. Community PRs adding a new
instrumentor extend the catalog with their framework's typical span shape;
the test stays the same.

Why a single generic harness:
  * Tier-2 maintenance is community-driven, not core. We want a fast,
    additive contribution path: "submit a span sample → land an integration."
  * Per-framework AC tests for ten frameworks would be a high-friction
    review surface; the canonical contract (§5) is the real specification.

This test is **not** an exhaustive instrumentation test — that's not the
point. It's a regression fence for the normalizer's coverage of the
attribute namespaces each Tier-2 framework uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hfao.compute.causal.replay_support import (
    REPLAY_SUPPORTED_FRAMEWORKS,
    REPLAY_UNSUPPORTED_FRAMEWORKS,
)
from hfao.ingest.normalize import normalize
from hfao.schema.events import Observation
from hfao.schema.otlp import Span
from hfao.storage.duckdb_backend import DuckDBBackend

_BASE_TIME = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Tier2SpanSample:
    """One canonical-shape span sample from a Tier-2 framework.

    Contributors fill ``attributes`` with whatever their instrumentor emits;
    the harness asserts the normalizer produces a populated
    :class:`Observation` with the right ``type``.
    """

    framework: str
    attributes: dict[str, Any]
    expected_type: str
    name: str = "step"
    resource_attributes: dict[str, Any] = field(default_factory=dict)
    replay_supported: bool | None = None


# A small starter catalog covering each Tier-2 family per §12.2.
# Community PRs extend this list; the test below is parameterised over it.
TIER2_CATALOG: list[Tier2SpanSample] = [
    # CrewAI — OpenInference-style spans
    Tier2SpanSample(
        framework="crewai",
        name="crewai.task.execute",
        attributes={
            "openinference.span.kind": "AGENT",
            "input.value": '{"question": "What is 2+2?"}',
            "output.value": '{"answer": "4"}',
            "agent.id": "math-agent",
            "agent.name": "MathAgent",
            "framework": "crewai",
        },
        expected_type="AGENT",
        replay_supported=False,
    ),
    # AutoGen
    Tier2SpanSample(
        framework="autogen",
        name="autogen.assistant.invoke",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o",
            "input.value": "Solve this",
            "output.value": "Done",
            "framework": "autogen",
        },
        expected_type="GENERATION",
        replay_supported=False,
    ),
    # DSPy
    Tier2SpanSample(
        framework="dspy",
        name="dspy.predict",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "claude-haiku-4-5",
            "input.value": "predict",
            "output.value": "result",
            "framework": "dspy",
        },
        expected_type="GENERATION",
        replay_supported=False,
    ),
    # LlamaIndex — RAG retrieval
    Tier2SpanSample(
        framework="llama_index",
        name="llama_index.retrieve",
        attributes={
            "openinference.span.kind": "RETRIEVER",
            "input.value": "query",
            "output.value": '[{"doc": "chunk-1"}]',
            "framework": "llama_index",
        },
        expected_type="RETRIEVAL",
        replay_supported=False,
    ),
    # Haystack
    Tier2SpanSample(
        framework="haystack",
        name="haystack.pipeline.run",
        attributes={
            "openinference.span.kind": "CHAIN",
            "input.value": "pipeline input",
            "output.value": "pipeline output",
            "framework": "haystack",
        },
        expected_type="SPAN",
        replay_supported=False,
    ),
    # Pydantic AI — uses OTel GenAI directly
    Tier2SpanSample(
        framework="pydantic_ai",
        name="chat",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.response.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 42,
            "gen_ai.usage.output_tokens": 13,
            "framework": "pydantic_ai",
        },
        expected_type="GENERATION",
        replay_supported=False,
    ),
    # Google ADK
    Tier2SpanSample(
        framework="google_adk",
        name="adk.agent.run",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.id": "adk-agent-1",
            "gen_ai.agent.name": "ADKAgent",
            "framework": "google_adk",
        },
        expected_type="AGENT",
        replay_supported=False,
    ),
    # AWS Strands
    Tier2SpanSample(
        framework="aws_strands",
        name="strands.flow.step",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "StrandsFlow",
            "framework": "aws_strands",
        },
        expected_type="AGENT",
        replay_supported=False,
    ),
    # LiteLLM — proxy/router; uses OpenInference LLM kind
    Tier2SpanSample(
        framework="litellm",
        name="litellm.completion",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "claude-haiku-4-5",
            "llm.token_count.prompt": 100,
            "llm.token_count.completion": 50,
            "framework": "litellm",
        },
        expected_type="GENERATION",
        replay_supported=False,
    ),
    # MCP-as-instrumentation
    Tier2SpanSample(
        framework="mcp",
        name="mcp.tool.call",
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": "search",
            "input.value": '{"q":"hello"}',
            "output.value": '{"hits": 3}',
            "framework": "mcp",
        },
        expected_type="TOOL",
        replay_supported=False,
    ),
]


def _span_from(sample: Tier2SpanSample, *, suffix: str) -> Span:
    """Materialise a flattened OTLP Span from a Tier-2 sample."""
    attrs = dict(sample.attributes)
    attrs.setdefault("hfao.project_id", "p1")
    return Span(
        trace_id=f"trace-{sample.framework}-{suffix}",
        span_id=f"span-{sample.framework}-{suffix}",
        parent_span_id=None,
        name=sample.name,
        start_time=_BASE_TIME,
        end_time=_BASE_TIME + timedelta(milliseconds=80),
        status="ok",
        attributes=attrs,
        resource_attributes=dict(sample.resource_attributes),
    )


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[DuckDBBackend]:
    b = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    b.init_schema()
    yield b
    b.close()


# --------------------------------------------------------------------------- #
# Generic harness — parameterized over the catalog
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sample",
    TIER2_CATALOG,
    ids=[s.framework for s in TIER2_CATALOG],
)
def test_tier2_span_normalizes_and_round_trips(
    sample: Tier2SpanSample, backend: DuckDBBackend
) -> None:
    """Every Tier-2 sample must normalize to a canonical Observation and
    round-trip through the backend unchanged on the identity/type axis."""
    span = _span_from(sample, suffix="rt")

    observations = normalize(span, default_project_id="p1")
    assert len(observations) == 1
    obs = observations[0]
    assert isinstance(obs, Observation)
    assert obs.project_id == "p1"
    assert obs.type == sample.expected_type, (
        f"{sample.framework}: normalizer produced type={obs.type}, "
        f"expected {sample.expected_type}"
    )
    # The normalizer may override the OTLP span name with
    # ``gen_ai.operation.name`` per §5.2; observation.name is therefore
    # informational and not asserted for the OTel-GenAI samples.
    assert obs.name

    # Round-trip through the backend.
    assert backend.write_events([obs]) == 1
    fetched = backend.get_trace("p1", obs.trace_id)
    assert len(fetched) == 1
    rt = fetched[0]
    assert rt.observation_id == obs.observation_id
    assert rt.type == obs.type
    assert rt.name == obs.name


@pytest.mark.parametrize(
    "sample",
    TIER2_CATALOG,
    ids=[s.framework for s in TIER2_CATALOG],
)
def test_tier2_replay_flag_is_honest(sample: Tier2SpanSample) -> None:
    """Every Tier-2 framework must be in the unsupported registry (or marked
    explicitly). The catalog asserts ``replay_supported=False`` for the
    Tier-2 set; the registry is the source of truth."""
    assert sample.framework in REPLAY_UNSUPPORTED_FRAMEWORKS, (
        f"{sample.framework} should be in REPLAY_UNSUPPORTED_FRAMEWORKS; "
        f"Tier-2 frameworks do not support counterfactual replay per §12.2 / Q-12"
    )
    assert sample.framework not in REPLAY_SUPPORTED_FRAMEWORKS


def test_tier2_catalog_covers_q12_set() -> None:
    """Sanity: the catalog covers every Tier-2 framework named in §16 Q-12."""
    expected = {
        "crewai",
        "autogen",
        "dspy",
        "llama_index",
        "haystack",
        "pydantic_ai",
        "google_adk",
        "aws_strands",
        "litellm",
        "mcp",
    }
    catalog_frameworks = {s.framework for s in TIER2_CATALOG}
    missing = expected - catalog_frameworks
    extra = catalog_frameworks - expected
    assert not missing, f"missing Tier-2 frameworks in catalog: {missing}"
    assert not extra, f"unexpected Tier-2 frameworks in catalog: {extra}"


def test_tier2_harness_extends_via_append() -> None:
    """Document the contributor contract: adding a sample is one append."""
    initial_len = len(TIER2_CATALOG)
    custom = Tier2SpanSample(
        framework="dspy",  # reuse — we don't mutate the module-level catalog
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "x",
            "framework": "dspy",
        },
        expected_type="GENERATION",
    )
    extended = [*TIER2_CATALOG, custom]
    assert len(extended) == initial_len + 1
    # The catalog stays immutable across tests (frozen dataclass + module
    # constant); contributors edit it via a PR, not at runtime.
    assert len(TIER2_CATALOG) == initial_len
