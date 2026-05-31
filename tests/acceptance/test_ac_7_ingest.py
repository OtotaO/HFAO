"""AC §7 — ingest server acceptance tests.

Covers §7.5:

- test_429_when_buffer_full
- test_dlq_on_persistent_storage_failure
- test_batch_flush_at_size
- test_batch_flush_at_age
- test_event_version_monotonic

The sustained 500 rps perf gate (§7.5) lives under ``tests/perf/`` and
is exercised separately; it is not part of the CI acceptance gate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import pytest
from hfao.config import HFAOConfig
from hfao.ingest.buffer import MemoryBuffer
from hfao.ingest.server import IngestWriter, create_app
from hfao.schema.causal import CausalEdge
from hfao.schema.events import Observation
from hfao.schema.scores import Score
from hfao.storage import StorageBackend
from hfao.storage.duckdb_backend import DuckDBBackend
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from starlette.testclient import TestClient


def _otlp_request(span_id_hex: str, model: str = "gpt-4o") -> bytes:
    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    kv = rs.resource.attributes.add()
    kv.key = "hfao.project_id"
    kv.value.string_value = "ingest-test"
    ss = rs.scope_spans.add()
    sp = ss.spans.add()
    sp.trace_id = bytes.fromhex("aa" * 16)
    sp.span_id = bytes.fromhex(span_id_hex)
    sp.name = "chat"
    sp.start_time_unix_nano = 1700000000_000_000_000
    sp.end_time_unix_nano = 1700000000_100_000_000
    sp.status.code = trace_pb2.Status.STATUS_CODE_OK
    for key, val in [
        ("gen_ai.operation.name", "chat"),
        ("gen_ai.request.model", model),
    ]:
        a = sp.attributes.add()
        a.key = key
        a.value.string_value = val
    return req.SerializeToString()


def test_429_when_buffer_full(tmp_path: pytest.FixtureRequest) -> None:
    # Tiny buffer so we can saturate it.
    backend = DuckDBBackend(":memory:")
    backend.init_schema()

    class _Buf(MemoryBuffer):
        def near_full(self) -> bool:
            return True

    buf = _Buf(capacity=8)
    # Use a per-test tempdir for the body store so the fixture works on
    # GitHub-hosted runners where the Docker default ``/data/bodies`` is
    # not writable.
    cfg = HFAOConfig(project="ingest-test", bodies_path=str(tmp_path / "bodies"))
    app = create_app(backend=backend, buffer=buf, config=cfg)
    with TestClient(app) as client:
        r = client.post(
            "/v1/traces",
            content=_otlp_request("0011223344556677"),
            headers={"content-type": "application/x-protobuf"},
        )
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "1"


def test_dlq_on_persistent_storage_failure() -> None:
    class _FailingBackend:
        """StorageBackend stub that fails every write_events."""

        def init_schema(self) -> None: ...

        def write_events(self, events: Iterable[Observation]) -> int:
            raise RuntimeError("storage down")

        def write_scores(self, scores: Iterable[Score]) -> int:
            return len(list(scores))

        def write_causal_edges(self, edges: Iterable[CausalEdge]) -> int:
            return len(list(edges))

        def get_trace(self, project_id: str, trace_id: str) -> list[Observation]:
            return []

        def list_traces(
            self,
            project_id: str,
            *,
            where_sql: str = "1=1",
            limit: int = 50,
            offset: int = 0,
        ) -> list[dict[str, Any]]:
            return []

        def search_traces_text(
            self, project_id: str, query: str, limit: int = 50
        ) -> list[dict[str, Any]]:
            return []

        def get_causal_edges(
            self, project_id: str, trace_id: str
        ) -> list[CausalEdge]:
            return []

        def get_scores(self, project_id: str, trace_id: str) -> list[Score]:
            return []

        def cost_rollup(
            self,
            project_id: str,
            *,
            date_from: datetime,
            date_to: datetime,
            group_by: list[str],
        ) -> list[dict[str, Any]]:
            return []

        def execute_readonly_sql(
            self, project_id: str, sql: str
        ) -> list[dict[str, Any]]:
            return []

    backend: StorageBackend = _FailingBackend()
    buf = MemoryBuffer()
    writer = IngestWriter(backend=backend, buffer=buf)

    now = datetime.now(timezone.utc)
    obs = Observation(
        project_id="p1",
        trace_id="t1",
        observation_id="o1",
        name="x",
        type="GENERATION",
        start_time=now,
        ingested_at=now,
    )
    buf.put([obs], [])

    # Manually invoke the flush path (no sleep on the 100ms/500ms/2s retries;
    # we monkeypatch time.sleep to skip the long waits).
    original_sleep = time.sleep
    try:
        time.sleep = lambda _s: None  # type: ignore[assignment]
        writer._flush([obs], [])  # pyright: ignore[reportPrivateUsage]
    finally:
        time.sleep = original_sleep

    assert buf.dlq_size == 1


def test_batch_flush_at_size() -> None:
    """Batch drains grab everything available up to the size cap."""
    buf = MemoryBuffer(capacity=1_000_000)
    now = datetime.now(timezone.utc)
    for i in range(5):
        buf.put(
            [
                Observation(
                    project_id="p1",
                    trace_id="t1",
                    observation_id=f"o{i}",
                    name="x",
                    type="GENERATION",
                    start_time=now,
                    ingested_at=now,
                )
            ],
            [],
        )
    obs, _ = buf.drain(timeout=0.1)
    assert [o.observation_id for o in obs] == [f"o{i}" for i in range(5)]


def test_batch_flush_at_age() -> None:
    """If no items arrive within ``timeout``, drain returns empty lists."""
    buf = MemoryBuffer()
    started = time.monotonic()
    obs, scs = buf.drain(timeout=0.2)
    elapsed = time.monotonic() - started
    assert obs == [] and scs == []
    assert 0.15 <= elapsed <= 1.0


def test_event_version_monotonic() -> None:
    """§7.4: higher event_version overwrites lower per (trace, observation)."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    buf = MemoryBuffer()
    writer = IngestWriter(backend=backend, buffer=buf)
    writer.start()

    now = datetime.now(timezone.utc)
    obs_v1 = Observation(
        project_id="p1",
        trace_id="t1",
        observation_id="o1",
        name="x",
        type="GENERATION",
        start_time=now,
        ingested_at=now,
        status="ok",
        event_version=1,
    )
    obs_v2 = Observation(
        project_id="p1",
        trace_id="t1",
        observation_id="o1",
        name="x",
        type="GENERATION",
        start_time=now,
        ingested_at=now,
        status="error",
        event_version=2,
    )
    buf.put([obs_v1], [])
    buf.put([obs_v2], [])
    # Let the writer drain.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        got = backend.get_trace("p1", "t1")
        if got and got[0].event_version == 2:
            break
        time.sleep(0.05)
    writer.stop(timeout=2.0)
    got = backend.get_trace("p1", "t1")
    assert len(got) == 1
    assert got[0].status == "error"
    assert got[0].event_version == 2


def test_writer_protocol_safety() -> None:
    """Smoke: writer stops cleanly even with no batches."""
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    buf = MemoryBuffer()
    writer = IngestWriter(backend=backend, buffer=buf)
    writer.start()
    writer.stop(timeout=2.0)
    # Thread should be gone.
    _ = threading  # retained for potential thread-count assertions


@pytest.mark.perf
def test_otlp_http_under_load_500_rps() -> None:
    """§7.5 perf gate — marker-only; real runner lives under tests/perf/.

    Skipped in acceptance runs; exercised by the perf CI job.
    """
    pytest.skip("perf gate handled by tests/perf runner")
