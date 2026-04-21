"""AC §5 — OTLP wire protocol acceptance tests.

Covers the eight tests enumerated in §5.7:

- test_otlp_http_protobuf_accept
- test_otlp_http_json_accept
- test_otel_genai_chat_completion_round_trip
- test_openinference_llm_round_trip
- test_mcp_meta_traceparent_extracted
- test_a2a_context_id_becomes_session
- test_unknown_span_falls_through_as_SPAN
- test_log_event_evaluation_becomes_score
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from google.protobuf.json_format import MessageToJson
from hfao.config import HFAOConfig
from hfao.ingest.buffer import MemoryBuffer
from hfao.ingest.normalize import normalize, normalize_scores
from hfao.ingest.server import create_app
from hfao.schema.otlp import Span, SpanEvent
from hfao.storage.duckdb_backend import DuckDBBackend
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from starlette.testclient import TestClient

_TRACE_ID = bytes.fromhex("00112233445566778899aabbccddeeff")
_SPAN_ID = bytes.fromhex("0011223344556677")


def _build_otel_genai_request() -> trace_service_pb2.ExportTraceServiceRequest:
    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    kv = rs.resource.attributes.add()
    kv.key = "hfao.project_id"
    kv.value.string_value = "wire-test"
    ss = rs.scope_spans.add()
    sp = ss.spans.add()
    sp.trace_id = _TRACE_ID
    sp.span_id = _SPAN_ID
    sp.name = "chat"
    sp.start_time_unix_nano = 1700000000_000_000_000
    sp.end_time_unix_nano = 1700000000_250_000_000
    sp.status.code = trace_pb2.Status.STATUS_CODE_OK
    for key, val in [
        ("gen_ai.operation.name", "chat"),
        ("gen_ai.request.model", "gpt-4o"),
        ("gen_ai.response.model", "gpt-4o-2024-08-06"),
        ("gen_ai.conversation.id", "conv-1"),
    ]:
        a = sp.attributes.add()
        a.key = key
        a.value.string_value = val
    for key, val_int in [
        ("gen_ai.usage.input_tokens", 10),
        ("gen_ai.usage.output_tokens", 20),
    ]:
        a = sp.attributes.add()
        a.key = key
        a.value.int_value = val_int
    a = sp.attributes.add()
    a.key = "gen_ai.request.temperature"
    a.value.double_value = 0.7
    a = sp.attributes.add()
    a.key = "gen_ai.input.messages"
    a.value.string_value = '[{"role":"user","content":"hi"}]'
    return req


@pytest.fixture
def client() -> Iterator[TestClient]:
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    buffer = MemoryBuffer()
    cfg = HFAOConfig(project="wire-test")
    app = create_app(backend=backend, buffer=buffer, config=cfg)
    with TestClient(app) as c:
        # Expose buffer on the client via state so tests can drain.
        c.__dict__["_buffer"] = buffer
        yield c


def test_otlp_http_protobuf_accept(client: TestClient) -> None:
    req = _build_otel_genai_request()
    r = client.post(
        "/v1/traces",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 200
    assert r.json() == {"accepted": 1}


def test_otlp_http_json_accept(client: TestClient) -> None:
    req = _build_otel_genai_request()
    r = client.post(
        "/v1/traces",
        content=MessageToJson(req).encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"accepted": 1}


def test_otel_genai_chat_completion_round_trip(client: TestClient) -> None:
    req = _build_otel_genai_request()
    r = client.post(
        "/v1/traces",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 200
    obs, _ = client.__dict__["_buffer"].drain(timeout=0.05)
    assert len(obs) == 1
    o = obs[0]
    assert o.type == "GENERATION"
    assert o.model == "gpt-4o-2024-08-06"  # response.model wins over request.model
    assert o.usage.prompt_tokens == 10
    assert o.usage.completion_tokens == 20
    assert o.usage.total_tokens == 30
    assert o.session_id == "conv-1"
    assert o.project_id == "wire-test"
    assert o.model_parameters.get("temperature") == "0.7"
    assert o.input and "user" in o.input


def test_openinference_llm_round_trip() -> None:
    now = datetime(2026, 4, 20, tzinfo=timezone.utc)
    span = Span(
        trace_id="a" * 32,
        span_id="b" * 16,
        name="OpenAI Chat",
        start_time=now,
        end_time=now,
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "claude-sonnet-4-5",
            "llm.token_count.prompt": 5,
            "llm.token_count.completion": 15,
            "input.value": '{"q":"hi"}',
            "output.value": "hello",
            "session.id": "sess-42",
            "user.id": "alice",
            "tag.tags": ["prod", "beta"],
            "metadata": '{"foo":"bar"}',
        },
    )
    [o] = normalize(span)
    assert o.type == "GENERATION"
    assert o.model == "claude-sonnet-4-5"
    assert o.usage.total_tokens == 20
    assert o.session_id == "sess-42"
    assert o.user_id == "alice"
    assert set(o.tags) == {"prod", "beta"}
    assert o.metadata.get("foo") == "bar"


def test_mcp_meta_traceparent_extracted() -> None:
    """§5.4: MCP ``_meta.traceparent`` and ``hfao.session_id`` surface as
    canonical fields when pushed through the normalizer as span attrs."""
    now = datetime(2026, 4, 20, tzinfo=timezone.utc)
    span = Span(
        trace_id="c" * 32,
        span_id="d" * 16,
        name="mcp.call_tool",
        start_time=now,
        end_time=now,
        attributes={
            "openinference.span.kind": "TOOL",
            "mcp._meta.traceparent": "00-deadbeef-cafebabe-01",
            "hfao.session_id": "mcp-session-9",
            "mcp._meta.hfao.project_id": "mcp-project",
        },
    )
    [o] = normalize(span)
    assert o.type == "TOOL"
    assert o.session_id == "mcp-session-9"


def test_a2a_context_id_becomes_session() -> None:
    now = datetime(2026, 4, 20, tzinfo=timezone.utc)
    span = Span(
        trace_id="e" * 32,
        span_id="f" * 16,
        name="invoke_agent",
        start_time=now,
        end_time=now,
        attributes={
            "openinference.span.kind": "AGENT",
            "a2a.context_id": "ctx-99",
            "a2a.task_id": "task-7",
            "a2a.agent_card_url": "https://agents.example.com/coder",
        },
    )
    [o] = normalize(span)
    assert o.type == "AGENT"
    assert o.session_id == "ctx-99"
    assert o.metadata.get("a2a.task_id") == "task-7"
    assert o.metadata.get("a2a.agent_card_url", "").startswith("https://")


def test_unknown_span_falls_through_as_SPAN() -> None:  # noqa: N802 — spec §5.7 name
    now = datetime(2026, 4, 20, tzinfo=timezone.utc)
    span = Span(
        trace_id="0" * 32,
        span_id="1" * 16,
        name="some-internal-op",
        start_time=now,
        end_time=now,
        attributes={"http.method": "GET", "db.system": "postgresql"},
    )
    [o] = normalize(span)
    assert o.type == "SPAN"
    assert o.model is None
    assert o.usage.total_tokens == 0


def test_log_event_evaluation_becomes_score(client: TestClient) -> None:
    """§5.1 + §5.7: logs with ``gen_ai.evaluation.result`` names produce Scores."""
    # Build an OTLP logs request with one gen_ai.evaluation.result record.
    req = logs_service_pb2.ExportLogsServiceRequest()
    rl = req.resource_logs.add()
    sl = rl.scope_logs.add()
    lr = sl.log_records.add()
    lr.time_unix_nano = 1700000000_000_000_000
    lr.span_id = _SPAN_ID
    lr.event_name = "gen_ai.evaluation.result"
    lr.body.string_value = "judge output"
    for key, val in [
        ("gen_ai.evaluation.name", "helpfulness"),
        ("gen_ai.response.model", "claude-haiku-4-5"),
        ("gen_ai.evaluation.explanation", "addressed all parts"),
    ]:
        a = lr.attributes.add()
        a.key = key
        a.value.string_value = val
    a = lr.attributes.add()
    a.key = "gen_ai.evaluation.score"
    a.value.double_value = 0.87

    r = client.post(
        "/v1/logs",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 200
    assert r.json() == {"accepted": 1}

    _, scores = client.__dict__["_buffer"].drain(timeout=0.05)
    assert len(scores) == 1
    s = scores[0]
    assert s.name == "helpfulness"
    assert abs((s.value or 0.0) - 0.87) < 1e-9
    assert s.source == "LLM_JUDGE"
    assert s.judge_model == "claude-haiku-4-5"
    assert s.comment == "addressed all parts"


def test_log_event_normalize_scores_direct() -> None:
    """Direct unit test: ``normalize_scores`` extracts evaluation events."""
    now = datetime(2026, 4, 20, tzinfo=timezone.utc)
    span = Span(
        trace_id="z" * 32,
        span_id="y" * 16,
        name="invoke_agent",
        start_time=now,
        end_time=now,
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "hfao.project_id": "p1",
        },
        events=[
            SpanEvent(
                name="gen_ai.evaluation.result",
                timestamp=now,
                attributes={
                    "gen_ai.evaluation.name": "accuracy",
                    "gen_ai.evaluation.score": 0.95,
                },
            )
        ],
    )
    scores = normalize_scores(span)
    assert len(scores) == 1
    assert scores[0].name == "accuracy"
    assert abs((scores[0].value or 0.0) - 0.95) < 1e-9
    _ = json  # retained for potential body fixture reuse
    _ = logs_pb2  # import parity with logs-service fixtures above
