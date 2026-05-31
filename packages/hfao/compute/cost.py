"""Cost rollup engine (SPEC §8.3).

The ``cost_daily`` rollup feeds the cockpit Costs tab (PR #10) and the §9.2
``get_cost_by`` MCP tool. DuckDB needs a periodic refresh worker because the
table is rebuilt by an aggregation query (the single-binary shape doesn't run
a streaming aggregator); ClickHouse uses a ``SummingMergeTree`` MV that
auto-refreshes.

This module is a thin façade over :meth:`StorageBackend.refresh_cost_rollup`
plus a worker loop. The aggregation SQL lives in the backend (Appendix C
rule 4 — no SQL outside ``packages/hfao/storage``).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hfao.storage import StorageBackend


log = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class RefreshResult:
    """What :func:`refresh` returns. ``row_count`` is informational."""

    row_count: int
    duration_ms: float
    refreshed_at: datetime


def refresh(backend: StorageBackend) -> RefreshResult:
    """Rebuild the cost rollup once. Cross-backend safe (ClickHouse no-op)."""
    started = time.perf_counter()
    rows = backend.refresh_cost_rollup()
    duration_ms = (time.perf_counter() - started) * 1000
    log.debug("cost rollup refreshed in %.1f ms (%d rows)", duration_ms, rows)
    return RefreshResult(
        row_count=rows,
        duration_ms=duration_ms,
        refreshed_at=datetime.now(timezone.utc),
    )


class CostRollupWorker:
    """Background thread that calls :func:`refresh` every ``interval_s``.

    Mirrors the pattern of :class:`hfao.ingest.buffer` workers: tiny,
    explicit, stoppable. Failures are logged and **do not** crash the loop —
    the rollup is best-effort, not a write path.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        interval_s: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._backend = backend
        self._interval_s = max(1, int(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result: RefreshResult | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hfao-cost-rollup", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def last_result(self) -> RefreshResult | None:
        return self._last_result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._last_result = refresh(self._backend)
            except Exception:  # noqa: BLE001 — log and keep ticking
                log.exception("cost rollup refresh failed; will retry")
            self._stop.wait(self._interval_s)


def staleness(result: RefreshResult | None) -> timedelta | None:
    """How long ago the last refresh ran. ``None`` means never."""
    if result is None:
        return None
    return datetime.now(timezone.utc) - result.refreshed_at


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "CostRollupWorker",
    "RefreshResult",
    "refresh",
    "staleness",
]
