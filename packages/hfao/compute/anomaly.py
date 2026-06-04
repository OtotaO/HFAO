"""Anomaly detection engine (SPEC §16 Q-18).

Surfaces patterns the user did **not** pre-define — the Q-9 "automatically
shares insights strategically" surface, complementing the user-configured
:mod:`hfao.compute.monitor` engine. Five detectors land in v1:

  * :class:`RollingMeanZScore` — two-sided Z against a rolling baseline.
  * :class:`WesternElectricRules` — classic control-chart heuristics
    (8-of-1 / 2-of-3 / 4-of-5 / 8-in-a-row).
  * :class:`KSDistance` — one-sample two-sided Kolmogorov–Smirnov against
    a baseline empirical distribution.
  * :class:`CalibrationDrift` — RMSE-trend of LLM judge vs. human annotations.
  * :class:`ReplayVerifiedScanner` — lifts Stage-2 ``COUNTERFACTUAL_REPLAY``
    edges (PR #19, Q-20) into ``Insight`` rows so the cockpit + MCP route
    replay-verified causes through the same insight pipeline as anomaly
    detections. Promised by Q-20 / Q-18 follow-up.

Detectors are **pure** — they consume a numeric series + optional baseline
and return zero or more :class:`Detection` records. The :class:`AnomalyEngine`
fetches the signal series via the backend's read-only SQL, applies the
detectors, and persists into the control plane via
:meth:`hfao.storage.control_plane.ControlPlane.record_insight`.

No new top-level dependencies — the stats are implemented inline (Z-score via
the normal CDF, KS via the standard supremum + asymptotic p-value).
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from hfao.schema.insights import (
    SEVERITY_RANK,
    InsightKind,
    InsightSeverity,
)

if TYPE_CHECKING:
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane


log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 5 * 60  # 5 minutes


# --------------------------------------------------------------------------- #
# Detection record (pre-persistence)
# --------------------------------------------------------------------------- #


def _empty_metadata() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class Detection:
    """The output of a detector. Engine converts these to control-plane rows."""

    kind: InsightKind
    severity: InsightSeverity
    signal_name: str
    baseline_value: float
    current_value: float
    confidence: float
    summary: str
    evidence_sql: str = ""
    trace_id: str | None = None
    metadata: dict[str, str] = field(default_factory=_empty_metadata)


# --------------------------------------------------------------------------- #
# Detector protocol + built-in detectors
# --------------------------------------------------------------------------- #


class Detector(Protocol):
    """A pure function from series + baseline to zero or more detections."""

    name: str

    def __call__(
        self,
        *,
        signal_name: str,
        series: Sequence[float],
        baseline: Sequence[float] | None = None,
    ) -> list[Detection]: ...


@dataclass
class RollingMeanZScore:
    """Two-sided Z against a rolling baseline mean.

    The baseline is the series excluding the trailing ``current_n`` points
    (which form the "current" subseries we test). If the absolute Z exceeds
    ``z_threshold``, emit a :class:`Detection` with severity scaled by Z.
    """

    name: str = "rolling_mean_zscore"
    z_threshold: float = 3.0
    current_n: int = 1

    def __call__(
        self,
        *,
        signal_name: str,
        series: Sequence[float],
        baseline: Sequence[float] | None = None,
    ) -> list[Detection]:
        if not series or len(series) < self.current_n + 5:
            return []
        current_slice = list(series[-self.current_n :])
        base_slice = (
            list(baseline)
            if baseline is not None
            else list(series[: -self.current_n])
        )
        if len(base_slice) < 5:
            return []
        mean = statistics.fmean(base_slice)
        sd = statistics.pstdev(base_slice)
        if sd <= 0:
            return []
        current_mean = statistics.fmean(current_slice)
        z = (current_mean - mean) / sd
        if abs(z) < self.z_threshold:
            return []
        severity = _severity_from_z(abs(z))
        confidence = max(0.0, min(1.0, 1.0 - 2 * (1 - _phi(abs(z)))))
        return [
            Detection(
                kind="trend_shift",
                severity=severity,
                signal_name=signal_name,
                baseline_value=mean,
                current_value=current_mean,
                confidence=confidence,
                summary=(
                    f"{signal_name}: current={current_mean:.4f} vs baseline "
                    f"mean={mean:.4f} (z={z:+.2f}, |z|≥{self.z_threshold:.1f})"
                ),
                metadata={"z": f"{z:+.4f}", "baseline_n": str(len(base_slice))},
            )
        ]


@dataclass
class WesternElectricRules:
    """Standard control-chart heuristics.

    Rule 1: any single point > 3σ from the mean.
    Rule 2: 2 of 3 consecutive points > 2σ on the same side.
    Rule 3: 4 of 5 consecutive points > 1σ on the same side.
    Rule 4: 8 consecutive points on the same side of the mean.
    """

    name: str = "western_electric"

    def __call__(
        self,
        *,
        signal_name: str,
        series: Sequence[float],
        baseline: Sequence[float] | None = None,
    ) -> list[Detection]:
        if not series or len(series) < 8:
            return []
        base = list(baseline) if baseline is not None else list(series)
        if len(base) < 8:
            return []
        mean = statistics.fmean(base)
        sd = statistics.pstdev(base)
        if sd <= 0:
            return []
        last = series[-1]
        tail8 = list(series[-8:])
        tail5 = list(series[-5:])
        tail3 = list(series[-3:])
        triggered: list[tuple[str, str]] = []  # (rule_id, summary)
        # Rule 1
        if abs(last - mean) > 3 * sd:
            triggered.append((
                "1",
                f"last point {last:.4f} > 3σ from mean {mean:.4f}",
            ))
        # Rule 4
        side = [1 if v > mean else -1 if v < mean else 0 for v in tail8]
        if side and abs(sum(side)) == 8:
            triggered.append((
                "4",
                f"8 consecutive points on the same side of mean {mean:.4f}",
            ))
        # Rule 2
        above = sum(1 for v in tail3 if v - mean > 2 * sd)
        below = sum(1 for v in tail3 if mean - v > 2 * sd)
        if above >= 2 or below >= 2:
            triggered.append((
                "2",
                "2 of 3 consecutive points > 2σ on the same side",
            ))
        # Rule 3
        above_5 = sum(1 for v in tail5 if v - mean > sd)
        below_5 = sum(1 for v in tail5 if mean - v > sd)
        if above_5 >= 4 or below_5 >= 4:
            triggered.append((
                "3",
                "4 of 5 consecutive points > 1σ on the same side",
            ))
        out: list[Detection] = []
        for rule_id, summary in triggered:
            severity: InsightSeverity = (
                "critical" if rule_id == "1" else "warning"
            )
            out.append(
                Detection(
                    kind="threshold_breach_implicit",
                    severity=severity,
                    signal_name=signal_name,
                    baseline_value=mean,
                    current_value=last,
                    confidence=0.9 if rule_id == "1" else 0.75,
                    summary=f"{signal_name}: Western Electric rule {rule_id}: {summary}",
                    metadata={"rule": rule_id, "sd": f"{sd:.4f}"},
                )
            )
        return out


@dataclass
class KSDistance:
    """One-sample two-sided Kolmogorov–Smirnov vs. a baseline distribution."""

    name: str = "ks_distance"
    p_threshold: float = 0.05
    current_n: int = 50

    def __call__(
        self,
        *,
        signal_name: str,
        series: Sequence[float],
        baseline: Sequence[float] | None = None,
    ) -> list[Detection]:
        if (
            not series
            or len(series) < self.current_n + 50
            or self.current_n < 5
        ):
            return []
        current_slice = list(series[-self.current_n :])
        base_slice = (
            list(baseline)
            if baseline is not None
            else list(series[: -self.current_n])
        )
        if len(base_slice) < 50:
            return []
        d_stat, p_value = _ks_two_sided(current_slice, base_slice)
        if p_value > self.p_threshold:
            return []
        baseline_mean = statistics.fmean(base_slice)
        current_mean = statistics.fmean(current_slice)
        confidence = max(0.0, min(1.0, 1.0 - p_value))
        return [
            Detection(
                kind="distribution_drift",
                severity=("critical" if p_value < 0.001 else "warning"),
                signal_name=signal_name,
                baseline_value=baseline_mean,
                current_value=current_mean,
                confidence=confidence,
                summary=(
                    f"{signal_name}: distribution drift (KS D={d_stat:.3f}, "
                    f"p={p_value:.4f}); baseline mean={baseline_mean:.4f}, "
                    f"current mean={current_mean:.4f}"
                ),
                metadata={
                    "ks_d": f"{d_stat:.4f}",
                    "p_value": f"{p_value:.6f}",
                    "current_n": str(len(current_slice)),
                },
            )
        ]


@dataclass
class CalibrationDrift:
    """Detects judge-vs-human RMSE trending up over rolling samples.

    Expects two equal-length series — judge scores and matched human scores
    on the same observations (the calibration pairing :func:`compute_bias`
    produces). When the latest window's RMSE exceeds the baseline's by
    ``rmse_multiplier``×, emit a ``calibration_drift`` insight.
    """

    name: str = "calibration_drift"
    rmse_multiplier: float = 1.5
    current_n: int = 20

    def __call__(
        self,
        *,
        signal_name: str,
        series: Sequence[float],
        baseline: Sequence[float] | None = None,
    ) -> list[Detection]:
        # `series` is the residuals (judge - human); the engine computes them.
        if len(series) < self.current_n + 5:
            return []
        current_slice = list(series[-self.current_n :])
        base_slice = (
            list(baseline)
            if baseline is not None
            else list(series[: -self.current_n])
        )
        if len(base_slice) < 5:
            return []
        baseline_rmse = math.sqrt(sum(r * r for r in base_slice) / len(base_slice))
        current_rmse = math.sqrt(sum(r * r for r in current_slice) / len(current_slice))
        if baseline_rmse <= 0:
            return []
        ratio = current_rmse / baseline_rmse
        if ratio < self.rmse_multiplier:
            return []
        return [
            Detection(
                kind="calibration_drift",
                severity=("critical" if ratio >= 2.0 else "warning"),
                signal_name=signal_name,
                baseline_value=baseline_rmse,
                current_value=current_rmse,
                confidence=max(0.0, min(1.0, 1.0 - 1.0 / ratio)),
                summary=(
                    f"{signal_name}: judge RMSE drifted "
                    f"{ratio:.2f}× (baseline {baseline_rmse:.3f} → "
                    f"current {current_rmse:.3f})"
                ),
                metadata={"ratio": f"{ratio:.4f}"},
            )
        ]


@dataclass
class ReplayVerifiedScanner:
    """Lifts Stage-2 ``COUNTERFACTUAL_REPLAY`` edges into Insights.

    Not a numeric detector — this scanner reads the ``causal_edges`` table via
    the storage backend's :meth:`StorageBackend.execute_readonly_sql` and
    emits one :class:`Detection` per fresh replay-verified edge. Per the
    Q-20 promise, replay-verified causes get a dedicated insight kind so the
    Q-19 routing layer can target them differently from raw anomalies.
    """

    name: str = "replay_verified"
    severity: InsightSeverity = "critical"

    def scan(
        self,
        *,
        backend: StorageBackend,
        project_id: str,
        since: datetime,
    ) -> list[Detection]:
        cutoff = since.strftime("%Y-%m-%d %H:%M:%S")
        sql = (
            "SELECT trace_id, source_observation_id, target_observation_id, "
            "confidence, evidence, computed_at "
            "FROM causal_edges "
            "WHERE method = 'COUNTERFACTUAL_REPLAY' "
            f"AND computed_at >= TIMESTAMP '{cutoff}'"
        )
        try:
            rows = backend.execute_readonly_sql(project_id, sql)
        except Exception:  # noqa: BLE001 — non-fatal: skip on backend hiccup
            log.exception("ReplayVerifiedScanner: backend read failed")
            return []
        out: list[Detection] = []
        for row in rows:
            confidence = float(row.get("confidence") or 0.0)
            trace_id = str(row.get("trace_id") or "")
            obs_id = str(row.get("target_observation_id") or "")
            evidence_text = str(row.get("evidence") or "")
            out.append(
                Detection(
                    kind="decisive_error_verified_by_replay",
                    severity=self.severity,
                    signal_name=f"trace:{trace_id}:obs:{obs_id}",
                    baseline_value=0.0,
                    current_value=confidence,
                    confidence=confidence,
                    summary=(
                        f"Counterfactual replay verified a decisive error "
                        f"in trace {trace_id}: {evidence_text[:120]}"
                    ),
                    evidence_sql=sql,
                    trace_id=trace_id,
                    metadata={"observation_id": obs_id},
                )
            )
        return out


# --------------------------------------------------------------------------- #
# Statistics primitives (no SciPy)
# --------------------------------------------------------------------------- #


def _phi(z: float) -> float:
    """Standard normal CDF via ``math.erfc``."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _severity_from_z(abs_z: float) -> InsightSeverity:
    if abs_z >= 5.0:
        return "critical"
    if abs_z >= 4.0:
        return "warning"
    if abs_z >= 3.0:
        return "notice"
    return "info"


def _ks_two_sided(sample: Sequence[float], baseline: Sequence[float]) -> tuple[float, float]:
    """Two-sample two-sided KS test. Returns ``(D, p_value)``.

    Implementation follows the standard asymptotic Kolmogorov distribution:
    ``D_n = sup|F_sample(x) - F_baseline(x)|`` and
    ``p ≈ 2·sum_{k=1..∞} (-1)^(k-1) e^(-2 k^2 λ^2)`` with
    ``λ = (sqrt(n) + 0.12 + 0.11/sqrt(n)) · D``, ``n = n_a·n_b/(n_a+n_b)``.
    """
    if not sample or not baseline:
        return 0.0, 1.0
    a = sorted(sample)
    b = sorted(baseline)
    na = len(a)
    nb = len(b)
    i = j = 0
    d = 0.0
    while i < na and j < nb:
        if a[i] <= b[j]:
            i += 1
        else:
            j += 1
        f_a = i / na
        f_b = j / nb
        d = max(d, abs(f_a - f_b))
    n_eff = na * nb / (na + nb)
    sqrt_n = math.sqrt(n_eff)
    lam = (sqrt_n + 0.12 + 0.11 / sqrt_n) * d
    if lam <= 0:
        return d, 1.0
    p = 0.0
    for k in range(1, 101):
        term = (-1) ** (k - 1) * math.exp(-2.0 * (k * lam) ** 2)
        p += term
        if abs(term) < 1e-12:
            break
    p = 2.0 * p
    return d, max(0.0, min(1.0, p))


# --------------------------------------------------------------------------- #
# Signal probe — what the AnomalyEngine asks the backend for
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SignalProbe:
    """A named SQL probe the engine runs to produce one numeric series.

    ``sql`` MUST return at least one numeric column named ``value``; the
    engine fetches every row and concatenates the ``value`` column into the
    series passed to detectors. ``baseline_sql`` is optional — when absent
    the detectors derive a baseline from the head of the series.
    """

    name: str
    sql: str
    baseline_sql: str | None = None


# Default probes the engine runs on every tick. Each maps onto a
# canonical KPI: error rate, total cost, p95 latency, total tokens. These
# rotate the same SQL the Q-14 monitor templates use, so a project that's
# already monitoring these signals gets the implicit-anomaly insights for
# free without extra configuration.
DEFAULT_PROBES: tuple[SignalProbe, ...] = (
    SignalProbe(
        name="error_rate_per_hour",
        sql=(
            "SELECT toStartOfHour(start_time) AS bucket, "
            "(count() FILTER (WHERE status = 'error') * 1.0) "
            "/ NULLIF(count(), 0) AS value "
            "FROM events_current "
            "WHERE start_time >= now() - INTERVAL '14 DAY' "
            "GROUP BY bucket ORDER BY bucket"
        ),
    ),
    SignalProbe(
        name="cost_per_hour",
        sql=(
            "SELECT toStartOfHour(start_time) AS bucket, "
            "COALESCE(SUM(total_cost_usd), 0.0) AS value "
            "FROM events_current "
            "WHERE start_time >= now() - INTERVAL '14 DAY' "
            "GROUP BY bucket ORDER BY bucket"
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Engine + worker
# --------------------------------------------------------------------------- #


def _empty_detector_list() -> list[Detector]:
    return []


def _empty_probe_list() -> list[SignalProbe]:
    return []


@dataclass
class AnomalyEngine:
    """Run all configured detectors against a project's signals, persist insights."""

    backend: StorageBackend
    control: ControlPlane
    detectors: list[Detector] = field(default_factory=_empty_detector_list)
    probes: list[SignalProbe] = field(default_factory=_empty_probe_list)
    replay_scanner: ReplayVerifiedScanner | None = None
    min_persist_severity: InsightSeverity = "info"

    def evaluate(self, *, project_id: str, now: datetime | None = None) -> list[dict[str, object]]:
        """Run one anomaly pass; persist anything at or above ``min_persist_severity``."""
        now = now or datetime.now(timezone.utc)
        persisted: list[dict[str, object]] = []
        for probe in self.probes:
            try:
                rows = self.backend.execute_readonly_sql(project_id, probe.sql)
            except Exception:  # noqa: BLE001 — engine never crashes on probe error
                log.exception(
                    "AnomalyEngine: probe %r failed for project %s",
                    probe.name,
                    project_id,
                )
                continue
            series = _series_from_rows(rows)
            baseline: list[float] | None = None
            if probe.baseline_sql:
                try:
                    base_rows = self.backend.execute_readonly_sql(
                        project_id, probe.baseline_sql
                    )
                    baseline = _series_from_rows(base_rows)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "AnomalyEngine: baseline probe %r failed", probe.name
                    )
                    baseline = None
            for detector in self.detectors:
                try:
                    detections = detector(
                        signal_name=probe.name, series=series, baseline=baseline
                    )
                except Exception:  # noqa: BLE001 — drop the detection but keep going
                    log.exception(
                        "AnomalyEngine: detector %s failed on %s",
                        detector.name,
                        probe.name,
                    )
                    continue
                for det in detections:
                    persisted.extend(
                        self._persist_unique(
                            project_id=project_id,
                            detection=det,
                            evidence_sql=probe.sql,
                            now=now,
                        )
                    )
        # Replay-verified scanner (Stage-2 edges → insights)
        if self.replay_scanner is not None:
            since = now - timedelta(days=1)
            for det in self.replay_scanner.scan(
                backend=self.backend, project_id=project_id, since=since
            ):
                persisted.extend(
                    self._persist_unique(
                        project_id=project_id,
                        detection=det,
                        evidence_sql=det.evidence_sql,
                        now=now,
                    )
                )
        return persisted

    def _persist_unique(
        self,
        *,
        project_id: str,
        detection: Detection,
        evidence_sql: str,
        now: datetime,
    ) -> list[dict[str, object]]:
        if SEVERITY_RANK[detection.severity] < SEVERITY_RANK[self.min_persist_severity]:
            return []
        day = now.strftime("%Y-%m-%d")
        if self.control.already_seen_insight(
            project_id=project_id,
            signal_name=detection.signal_name,
            kind=detection.kind,
            observed_day=day,
        ):
            return []
        row = self.control.record_insight(
            project_id=project_id,
            kind=detection.kind,
            severity=detection.severity,
            signal_name=detection.signal_name,
            baseline_value=detection.baseline_value,
            current_value=detection.current_value,
            observed_at=now.isoformat(),
            evidence_sql=detection.evidence_sql or evidence_sql,
            confidence=detection.confidence,
            summary=detection.summary,
            trace_id=detection.trace_id,
            metadata=detection.metadata,
        )
        return [row]


def _series_from_rows(rows: Iterable[dict[str, object]]) -> list[float]:
    out: list[float] = []
    for row in rows:
        v = row.get("value")
        if v is None:
            continue
        try:
            out.append(float(v))  # type: ignore[arg-type] — DB read returns object
        except (TypeError, ValueError):
            continue
    return out


class AnomalyWorker:
    """Background thread that ticks :meth:`AnomalyEngine.evaluate` every
    ``interval_s`` for the given ``project_id``. Mirrors
    :class:`hfao.compute.cost.CostRollupWorker` etc."""

    def __init__(
        self,
        engine: AnomalyEngine,
        *,
        project_id: str,
        interval_s: int = DEFAULT_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._project_id = project_id
        self._interval_s = max(60, int(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_count: int = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hfao-anomaly", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def last_count(self) -> int:
        return self._last_count

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                persisted = self._engine.evaluate(project_id=self._project_id)
                self._last_count = len(persisted)
            except Exception:  # noqa: BLE001 — log + keep ticking
                log.exception("anomaly worker tick failed; will retry")
            self._stop.wait(self._interval_s)


def default_detectors() -> list[Detector]:
    """Return the default detector stack used by the cockpit + AnomalyWorker."""
    return [
        RollingMeanZScore(),
        WesternElectricRules(),
        KSDistance(),
        CalibrationDrift(),
    ]


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_PROBES",
    "AnomalyEngine",
    "AnomalyWorker",
    "CalibrationDrift",
    "Detection",
    "Detector",
    "KSDistance",
    "ReplayVerifiedScanner",
    "RollingMeanZScore",
    "SignalProbe",
    "WesternElectricRules",
    "default_detectors",
]
