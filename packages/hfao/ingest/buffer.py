"""Ingest buffer implementations.

SPEC §7.1–§7.3. The ingest path decouples the HTTP handlers (writers) from
the storage flush loop (readers) with a bounded buffer:

- ``MemoryBuffer`` — a ``queue.Queue`` of serialized batches, used by the
  single-binary shape. Bounded size; ``near_full()`` drives the HTTP 429.
- ``RedisBuffer`` — a Redis Streams backed buffer for the Docker/K8s shape,
  enabling horizontal scaling of ingest workers. Reads use a consumer
  group so multiple writers can drain the same stream without duplication.

Both buffers round-trip through msgspec msgpack for compactness. The DLQ
is part of the buffer so a failing writer can persist its batch without
waking a separate service.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

import msgspec

from hfao.schema.events import Observation
from hfao.schema.scores import Score

_BATCH_MAX_SIZE = 10_000
_BACKPRESSURE_PCT = 0.8


class _Batch(msgspec.Struct, frozen=True, kw_only=True):
    observations: list[Observation]
    scores: list[Score]


_ENCODER = msgspec.msgpack.Encoder()
_DECODER = msgspec.msgpack.Decoder(_Batch)


@runtime_checkable
class IngestBuffer(Protocol):
    def near_full(self) -> bool: ...

    def put(self, observations: list[Observation], scores: list[Score]) -> None: ...

    def drain(self, timeout: float) -> tuple[list[Observation], list[Score]]: ...

    def put_dlq(
        self, observations: Iterable[Observation], scores: Iterable[Score]
    ) -> None: ...

    @property
    def dlq_size(self) -> int: ...


class MemoryBuffer:
    """In-process FIFO buffer backed by ``queue.Queue``."""

    def __init__(self, capacity: int = 100_000) -> None:
        self._capacity = capacity
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max(capacity // 128, 8))
        self._dlq_lock = threading.Lock()
        self._dlq_obs: list[Observation] = []
        self._dlq_scores: list[Score] = []

    def near_full(self) -> bool:
        qsize = self._queue.qsize()
        maxsize = self._queue.maxsize or self._capacity
        return qsize / max(maxsize, 1) >= _BACKPRESSURE_PCT

    def put(self, observations: list[Observation], scores: list[Score]) -> None:
        if not observations and not scores:
            return
        payload = _ENCODER.encode(_Batch(observations=observations, scores=scores))
        self._queue.put(payload, block=True)

    def drain(self, timeout: float) -> tuple[list[Observation], list[Score]]:
        try:
            first = self._queue.get(timeout=timeout)
        except queue.Empty:
            return [], []
        batch = _DECODER.decode(first)
        obs, scs = list(batch.observations), list(batch.scores)
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            b = _DECODER.decode(nxt)
            obs.extend(b.observations)
            scs.extend(b.scores)
            if len(obs) >= _BATCH_MAX_SIZE:
                break
        return obs, scs

    def put_dlq(
        self, observations: Iterable[Observation], scores: Iterable[Score]
    ) -> None:
        with self._dlq_lock:
            self._dlq_obs.extend(observations)
            self._dlq_scores.extend(scores)

    @property
    def dlq_size(self) -> int:
        with self._dlq_lock:
            return len(self._dlq_obs) + len(self._dlq_scores)


class RedisBuffer:
    """Redis Streams backed buffer for the Docker/K8s shape.

    Uses ``XADD`` to enqueue msgpack-encoded batches and ``XREADGROUP`` to
    drain them with acknowledgement. The DLQ is a second Redis stream
    (``hfao:dlq``) so a crashed writer's pending ack doesn't lose the batch.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        stream: str = "hfao:ingest",
        dlq_stream: str = "hfao:dlq",
        group: str = "hfao-writers",
        consumer: str = "writer-0",
        max_len: int = 1_000_000,
    ) -> None:
        import redis  # noqa: PLC0415

        redis_any: Any = redis
        self._client = redis_any.Redis.from_url(redis_url, decode_responses=False)
        self._stream = stream
        self._dlq_stream = dlq_stream
        self._group = group
        self._consumer = consumer
        self._max_len = max_len
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self._client.xgroup_create(
                self._stream, self._group, id="$", mkstream=True
            )
        except Exception as e:  # noqa: BLE001
            if "BUSYGROUP" not in str(e):
                raise

    def near_full(self) -> bool:
        pending: Any = self._client.xlen(self._stream)
        try:
            depth = int(pending)
        except (TypeError, ValueError):
            return False
        return depth / max(self._max_len, 1) >= _BACKPRESSURE_PCT

    def put(self, observations: list[Observation], scores: list[Score]) -> None:
        if not observations and not scores:
            return
        payload = _ENCODER.encode(_Batch(observations=observations, scores=scores))
        self._client.xadd(
            self._stream,
            {"payload": payload},
            maxlen=self._max_len,
            approximate=True,
        )

    def drain(self, timeout: float) -> tuple[list[Observation], list[Score]]:
        block_ms = max(int(timeout * 1000), 1)
        resp: Any = self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=_BATCH_MAX_SIZE,
            block=block_ms,
        )
        if not resp:
            return [], []
        obs: list[Observation] = []
        scs: list[Score] = []
        ack_ids: list[bytes] = []
        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                ack_ids.append(entry_id)
                raw = fields.get(b"payload") or fields.get("payload")
                if not raw:
                    continue
                b = _DECODER.decode(raw)
                obs.extend(b.observations)
                scs.extend(b.scores)
        if ack_ids:
            self._client.xack(self._stream, self._group, *ack_ids)
        return obs, scs

    def put_dlq(
        self, observations: Iterable[Observation], scores: Iterable[Score]
    ) -> None:
        obs_list = list(observations)
        scs_list = list(scores)
        if not obs_list and not scs_list:
            return
        payload = _ENCODER.encode(
            _Batch(observations=obs_list, scores=scs_list)
        )
        self._client.xadd(self._dlq_stream, {"payload": payload})

    @property
    def dlq_size(self) -> int:
        depth: Any = self._client.xlen(self._dlq_stream)
        try:
            return int(depth)
        except (TypeError, ValueError):
            return 0


def make_buffer(redis_url: str | None, *, capacity: int = 100_000) -> IngestBuffer:
    """Factory: prefer Redis when a URL is configured, else in-process memory."""
    if redis_url:
        return RedisBuffer(redis_url)
    return MemoryBuffer(capacity=capacity)


__all__ = [
    "IngestBuffer",
    "MemoryBuffer",
    "RedisBuffer",
    "make_buffer",
]
