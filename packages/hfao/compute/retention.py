"""Retention worker (SPEC §6.4).

Daily cron that prunes the hot tier (events / scores / causal_edges) and
local body offload directory according to each project's
:class:`RetentionPolicy`. The warm-tier (HF Buckets via DuckLake) prune is
intentionally deferred to v1.1 per §16 Q-13 — auto-sync isn't wired in v1,
so there are no shards to prune; the manual ``hfao parquet export`` CLI
pushes data on demand.

Like :class:`hfao.compute.cost.CostRollupWorker`, this is a simple daemon:
non-fatal failures log and continue. Production wiring runs it on a
24-hour cadence; the ``run_once`` helper is what the AC tests + ``hfao
retention run`` CLI command call directly.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane


log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 24 * 60 * 60  # one day


def _empty_per_project() -> dict[str, dict[str, int]]:
    return {}


def _empty_errors() -> list[str]:
    return []


@dataclass(frozen=True)
class RetentionResult:
    """Aggregated outcome of one retention pass across all projects."""

    started_at: datetime
    finished_at: datetime
    per_project: dict[str, dict[str, int]] = field(default_factory=_empty_per_project)
    bodies_pruned: int = 0
    errors: list[str] = field(default_factory=_empty_errors)


def run_once(
    backend: StorageBackend,
    control: ControlPlane,
    *,
    now: datetime | None = None,
    bodies_root: Path | str | None = None,
) -> RetentionResult:
    """Run one full retention pass against every project with a policy.

    Projects without a stored policy are **skipped**, not auto-defaulted —
    callers create policies via the cockpit Settings tab or the
    ``hfao retention set`` CLI. Per-tier ``*_days`` value of ``0`` disables
    that tier (no-op).
    """
    started = now or datetime.now(timezone.utc)
    policies = control.list_retention_policies()
    per_project: dict[str, dict[str, int]] = {}
    errors: list[str] = []

    for policy in policies:
        if not policy.get("enabled"):
            continue
        project_id = str(policy["project_id"])
        hot_days = int(policy.get("hot_days") or 0)
        if hot_days <= 0:
            continue
        before = started - timedelta(days=hot_days)
        try:
            counts = backend.purge_old(project_id, before=before)
            per_project[project_id] = counts
        except Exception as exc:  # noqa: BLE001 — best-effort, never crash
            errors.append(f"{project_id}: {exc!s}")
            log.exception("retention purge failed for project %s", project_id)

    bodies_pruned = 0
    if bodies_root is not None:
        bodies_pruned = _prune_bodies(
            Path(bodies_root), started, policies
        )

    return RetentionResult(
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        per_project=per_project,
        bodies_pruned=bodies_pruned,
        errors=errors,
    )


def _prune_bodies(
    root: Path, now: datetime, policies: list[dict[str, Any]]
) -> int:
    """Walk ``{root}/{project_id}/...`` and delete trees older than
    ``bodies_days`` per project."""
    if not root.exists():
        return 0
    by_project: dict[str, int] = {
        str(p["project_id"]): int(p.get("bodies_days") or 0)
        for p in policies
        if p.get("enabled")
    }
    pruned = 0
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        days = by_project.get(project_dir.name)
        if days is None or days <= 0:
            continue
        cutoff = (now - timedelta(days=days)).timestamp()
        for child in project_dir.rglob("*"):
            if child.is_file() and child.stat().st_mtime < cutoff:
                try:
                    child.unlink()
                    pruned += 1
                except OSError:
                    log.warning("retention: failed to unlink %s", child)
        # Sweep up empty subdirs (best-effort).
        _remove_empty_dirs(project_dir)
    return pruned


def _remove_empty_dirs(root: Path) -> None:
    for sub in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if sub.is_dir() and not any(sub.iterdir()):
            with contextlib.suppress(OSError):
                sub.rmdir()


class RetentionWorker:
    """Daemon thread that runs :func:`run_once` every ``interval_s``."""

    def __init__(
        self,
        backend: StorageBackend,
        control: ControlPlane,
        *,
        interval_s: int = DEFAULT_INTERVAL_S,
        bodies_root: Path | str | None = None,
    ) -> None:
        self._backend = backend
        self._control = control
        self._interval_s = max(60, int(interval_s))  # never tighter than 1 min
        self._bodies_root = bodies_root
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result: RetentionResult | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hfao-retention", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def last_result(self) -> RetentionResult | None:
        return self._last_result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._last_result = run_once(
                    self._backend, self._control, bodies_root=self._bodies_root
                )
            except Exception:  # noqa: BLE001 — log + keep ticking
                log.exception("retention pass failed; will retry")
            self._stop.wait(self._interval_s)


__all__ = [
    "DEFAULT_INTERVAL_S",
    "RetentionResult",
    "RetentionWorker",
    "run_once",
]
