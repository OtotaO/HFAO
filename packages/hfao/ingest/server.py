"""HFAO ingest server.

SPEC §7. ASGI app fronted by Granian in production exposing:

- ``POST /v1/traces`` — OTLP/HTTP protobuf or JSON (§5.1)
- ``POST /v1/logs`` — OTLP logs; ``gen_ai.*`` events merge into parent spans
- ``GET /health`` — liveness

A background ``IngestWriter`` drains the in-process buffer in batches and
writes to the configured ``StorageBackend``. Backpressure on 80% buffer
fill returns 429 with ``Retry-After: 1`` (§7.3). ``buffer.py`` formalizes
the buffer interface and adds a Redis-backed variant in the next commit;
this commit uses a plain ``queue.Queue`` at the boundary.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hfao.config import HFAOConfig
from hfao.ingest.normalize import normalize, normalize_scores
from hfao.ingest.otlp_http import parse_logs, parse_traces
from hfao.ingest.redact import Redactor
from hfao.schema.events import Observation
from hfao.schema.otlp import Span, SpanEvent
from hfao.schema.scores import Score
from hfao.storage import StorageBackend

log = logging.getLogger(__name__)

_BATCH_MAX_SIZE = 10_000
_BATCH_MAX_BYTES = 32 * 1024 * 1024
_BATCH_MAX_AGE_S = 2.0
_RETRY_DELAYS_S = (0.1, 0.5, 2.0)
_BACKPRESSURE_PCT = 0.8
_BUFFER_CAPACITY = 100_000


class IngestBuffer:
    """Bounded in-process FIFO of ``Observation`` / ``Score`` batches.

    SPEC §7.3: queue depth drives backpressure; ``near_full()`` triggers
    429 at the HTTP layer. A richer buffer (Redis Streams) lands in the
    next commit.
    """

    def __init__(self, capacity: int = _BUFFER_CAPACITY) -> None:
        self._capacity = capacity
        self._queue: queue.Queue[tuple[list[Observation], list[Score]]] = queue.Queue(
            maxsize=max(capacity // 128, 8)
        )
        self._dropped = 0
        self._dlq_obs: list[Observation] = []
        self._dlq_scores: list[Score] = []

    def near_full(self) -> bool:
        qsize = self._queue.qsize()
        maxsize = self._queue.maxsize or self._capacity
        return qsize / max(maxsize, 1) >= _BACKPRESSURE_PCT

    def put(self, observations: list[Observation], scores: list[Score]) -> None:
        if not observations and not scores:
            return
        self._queue.put((observations, scores), block=True)

    def drain(self, timeout: float) -> tuple[list[Observation], list[Score]]:
        try:
            first = self._queue.get(timeout=timeout)
        except queue.Empty:
            return [], []
        obs, scs = list(first[0]), list(first[1])
        # Opportunistic: grab anything else immediately available without blocking.
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            obs.extend(nxt[0])
            scs.extend(nxt[1])
            if len(obs) >= _BATCH_MAX_SIZE:
                break
        return obs, scs

    def put_dlq(self, observations: Iterable[Observation], scores: Iterable[Score]) -> None:
        self._dlq_obs.extend(observations)
        self._dlq_scores.extend(scores)

    @property
    def dlq_size(self) -> int:
        return len(self._dlq_obs) + len(self._dlq_scores)


class IngestWriter:
    """Background thread that drains the buffer into the storage backend."""

    def __init__(self, backend: StorageBackend, buffer: IngestBuffer) -> None:
        self._backend = backend
        self._buffer = buffer
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hfao-ingest-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            obs, scs = self._buffer.drain(timeout=_BATCH_MAX_AGE_S)
            if not obs and not scs:
                continue
            self._flush(obs, scs)

    def _flush(self, observations: list[Observation], scores: list[Score]) -> None:
        last_err: Exception | None = None
        for delay in (0.0, *_RETRY_DELAYS_S):
            if delay:
                time.sleep(delay)
            try:
                if observations:
                    self._backend.write_events(observations)
                if scores:
                    self._backend.write_scores(scores)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("ingest flush failed (will retry): %s", e)
        log.error("ingest flush exhausted retries; sending to DLQ: %s", last_err)
        self._buffer.put_dlq(observations, scores)


def create_app(
    *,
    backend: StorageBackend,
    buffer: IngestBuffer,
    config: HFAOConfig,
    redactor: Redactor | None = None,
) -> Starlette:
    redactor = redactor or Redactor()

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def v1_traces(request: Request) -> Response:
        return await _handle_traces(
            request, buffer=buffer, config=config, redactor=redactor
        )

    async def v1_logs(request: Request) -> Response:
        return await _handle_logs(
            request,
            backend=backend,
            buffer=buffer,
            config=config,
            redactor=redactor,
        )

    return Starlette(
        debug=False,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/traces", v1_traces, methods=["POST"]),
            Route("/v1/logs", v1_logs, methods=["POST"]),
        ],
    )


async def _handle_traces(
    request: Request,
    *,
    buffer: IngestBuffer,
    config: HFAOConfig,
    redactor: Redactor,
) -> Response:
    if buffer.near_full():
        return JSONResponse(
            {"error": "buffer near full"},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    body = await request.body()
    if len(body) > config.ingest_max_body_bytes:
        return JSONResponse({"error": "body too large"}, status_code=413)
    try:
        spans = parse_traces(body, request.headers.get("content-type", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.exception("failed to parse OTLP traces")
        return JSONResponse({"error": f"malformed OTLP body: {e}"}, status_code=400)

    observations: list[Observation] = []
    scores: list[Score] = []
    for sp in spans:
        for obs in normalize(sp, default_project_id=config.project):
            observations.append(redactor.redact_observation(obs))
        scores.extend(normalize_scores(sp))
    buffer.put(observations, scores)
    return JSONResponse({"accepted": len(observations)})


async def _handle_logs(
    request: Request,
    *,
    backend: StorageBackend,
    buffer: IngestBuffer,
    config: HFAOConfig,
    redactor: Redactor,
) -> Response:
    _ = backend  # reserved: log-to-score merging will hit backend directly
    _ = redactor  # log scores carry no bodies, but the param keeps parity
    if buffer.near_full():
        return JSONResponse(
            {"error": "buffer near full"},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    body = await request.body()
    if len(body) > config.ingest_max_body_bytes:
        return JSONResponse({"error": "body too large"}, status_code=413)
    try:
        pairs = parse_logs(body, request.headers.get("content-type", ""))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"malformed OTLP body: {e}"}, status_code=400)

    # Re-wrap the log events as synthetic spans carrying only the event so
    # the normalizer's score extraction can reuse its existing logic.
    scores: list[Score] = []
    for span_id, ev in pairs:
        synthetic = _synthetic_span_for_event(span_id, ev, config.project)
        scores.extend(normalize_scores(synthetic))
    buffer.put([], scores)
    return JSONResponse({"accepted": len(scores)})


def _synthetic_span_for_event(span_id: str, ev: SpanEvent, project_id: str) -> Span:
    return Span(
        trace_id=str(ev.attributes.get("trace_id", "") or ""),
        span_id=span_id,
        name=ev.name,
        start_time=ev.timestamp,
        end_time=ev.timestamp,
        attributes={"hfao.project_id": project_id},
        events=[ev],
    )


def serve(config: HFAOConfig) -> None:  # pragma: no cover - boots granian
    """Boot Granian with the ingest app. Production entry point."""
    from hfao.storage.clickhouse_backend import ClickHouseBackend
    from hfao.storage.duckdb_backend import DuckDBBackend

    backend: StorageBackend
    if config.backend == "duckdb":
        d = DuckDBBackend(config.duckdb_path)
        d.init_schema()
        backend = d
    elif config.backend == "clickhouse":
        if not config.clickhouse_dsn:
            raise ValueError("HFAO_CLICKHOUSE_DSN must be set for clickhouse backend")
        c = ClickHouseBackend(config.clickhouse_dsn)
        c.init_schema()
        backend = c
    else:
        raise ValueError(f"Unknown backend: {config.backend}")

    buffer = IngestBuffer()
    writer = IngestWriter(backend=backend, buffer=buffer)
    writer.start()

    app = create_app(backend=backend, buffer=buffer, config=config)
    _run_granian(app, config)
    writer.stop()


_SERVE_APP: list[Starlette | None] = [None]


def granian_app() -> Starlette:  # pragma: no cover - invoked by granian
    app = _SERVE_APP[0]
    if app is None:
        raise RuntimeError("ingest app not initialized; call serve() first")
    return app


def _run_granian(app: Starlette, config: HFAOConfig) -> None:  # pragma: no cover
    import granian  # noqa: PLC0415

    # granian's Server API: pass a reference string; we stash the app in a
    # module attribute that Granian imports.
    _SERVE_APP[0] = app
    granian_cls: Any = granian.Granian
    granian_server = granian_cls(
        "hfao.ingest.server:granian_app",
        address=config.ingest_host,
        port=config.ingest_port,
        interface="asgi",
    )
    granian_server.serve()


__all__ = [
    "IngestBuffer",
    "IngestWriter",
    "create_app",
    "serve",
]
