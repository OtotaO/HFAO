"""AC §4.6 + §6.7 — body offload at 64 KiB.

Large ``input`` / ``output`` bodies are written to a ``BodyStore`` and the
inline column is replaced with empty + the URI goes to ``*_ref``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hfao.config import HFAOConfig
from hfao.ingest.body_offload import BodyOffloader, BodyStore, LocalBodyStore
from hfao.ingest.buffer import MemoryBuffer
from hfao.ingest.server import create_app
from hfao.schema.events import Observation
from hfao.storage.duckdb_backend import DuckDBBackend
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from starlette.testclient import TestClient


def _obs(now: datetime, *, input: str | None = None, output: str | None = None) -> Observation:  # noqa: A002 — spec §4.1 field
    return Observation(
        project_id="p1",
        trace_id="t1",
        observation_id="o1",
        name="x",
        type="GENERATION",
        start_time=now,
        ingested_at=now,
        input=input,
        output=output,
    )


def test_local_body_store_roundtrip(tmp_path: Path) -> None:
    store = LocalBodyStore(tmp_path)
    assert isinstance(store, BodyStore)
    uri = store.put(
        project_id="p1",
        trace_id="t1",
        observation_id="o1",
        field="input",
        body="a" * 100_000,
    )
    assert uri.startswith("file://")
    assert uri.endswith(".input.json.zst")
    got = store.get(uri)
    assert len(got) == 100_000
    assert got[:4] == "aaaa"


def test_body_offload_leaves_small_bodies_inline(tmp_path: Path) -> None:
    store = LocalBodyStore(tmp_path)
    off = BodyOffloader(store, threshold_bytes=64 * 1024)
    now = datetime.now(timezone.utc)
    obs = _obs(now, input="small", output="also small")
    got = off.offload(obs)
    assert got.input == "small"
    assert got.output == "also small"
    assert got.input_ref is None
    assert got.output_ref is None


def test_body_offload_at_64kb(tmp_path: Path) -> None:
    store = LocalBodyStore(tmp_path)
    off = BodyOffloader(store, threshold_bytes=64 * 1024)
    now = datetime.now(timezone.utc)

    # 65 KiB input triggers offload; 1 KiB output stays inline.
    big = "x" * (65 * 1024)
    obs = _obs(now, input=big, output="short")
    got = off.offload(obs)
    assert got.input == ""
    assert got.input_ref is not None
    assert got.input_ref.startswith("file://")
    assert got.output == "short"
    assert got.output_ref is None

    # Round-trip the offloaded body.
    assert store.get(got.input_ref) == big


def test_body_offload_path_sanitization(tmp_path: Path) -> None:
    store = LocalBodyStore(tmp_path)
    uri = store.put(
        project_id="../../../etc",
        trace_id="nope",
        observation_id="obs",
        field="input",
        body="y" * 70_000,
    )
    resolved = Path(uri.removeprefix("file://")).resolve()
    # Sanitized path must still live under the store root.
    assert str(resolved).startswith(str(tmp_path.resolve()))


def test_body_offload_end_to_end_via_http(tmp_path: Path) -> None:
    backend = DuckDBBackend(":memory:")
    backend.init_schema()
    buffer = MemoryBuffer()
    cfg = HFAOConfig(
        project="offload-test",
        bodies_path=str(tmp_path),
        body_offload_threshold_bytes=64 * 1024,
    )
    app = create_app(backend=backend, buffer=buffer, config=cfg)

    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    kv = rs.resource.attributes.add()
    kv.key = "hfao.project_id"
    kv.value.string_value = "offload-test"
    ss = rs.scope_spans.add()
    sp = ss.spans.add()
    sp.trace_id = bytes.fromhex("aa" * 16)
    sp.span_id = bytes.fromhex("bb" * 8)
    sp.name = "chat"
    sp.start_time_unix_nano = 1700000000_000_000_000
    sp.end_time_unix_nano = 1700000000_100_000_000
    sp.status.code = trace_pb2.Status.STATUS_CODE_OK
    for key, val in [
        ("gen_ai.operation.name", "chat"),
        ("gen_ai.input.messages", "y" * (70 * 1024)),
    ]:
        a = sp.attributes.add()
        a.key = key
        a.value.string_value = val

    with TestClient(app) as client:
        r = client.post(
            "/v1/traces",
            content=req.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
        )
    assert r.status_code == 200
    obs, _ = buffer.drain(timeout=0.05)
    assert len(obs) == 1
    assert obs[0].input == ""
    assert obs[0].input_ref is not None
    assert obs[0].input_ref.startswith("file://")
