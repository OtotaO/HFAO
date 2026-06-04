"""AC §8 / §16 Q-18 acceptance tests — Insights / anomaly engine.

Covers:

  - detector contracts: RollingMeanZScore, WesternElectricRules,
    KSDistance, CalibrationDrift, ReplayVerifiedScanner
  - statistics primitives (`_ks_two_sided`, `_phi`, `_severity_from_z`)
  - control-plane CRUD: record_insight, get_insight, list_insights,
    already_seen_insight (the dedup key the engine uses)
  - engine round-trip: probes → series → detector → insight rows persisted,
    with same-day dedup enforced
  - ReplayVerifiedScanner lifts Stage-2 COUNTERFACTUAL_REPLAY edges into
    insights (the Q-20 follow-up the Q-18 entry promised)
  - severity filter respects the ladder
  - worker lifecycle (start / stop / non-fatal failures)
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hfao.compute.anomaly import (
    AnomalyEngine,
    AnomalyWorker,
    CalibrationDrift,
    Detection,
    KSDistance,
    ReplayVerifiedScanner,
    RollingMeanZScore,
    SignalProbe,
    WesternElectricRules,
    _ks_two_sided,
    _phi,
    _severity_from_z,
    default_detectors,
)
from hfao.schema.causal import CausalEdge
from hfao.schema.events import CostBreakdown, Observation, TokenUsage
from hfao.schema.insights import SEVERITY_RANK, severity_ge
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend

_NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[DuckDBBackend]:
    b = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    b.init_schema()
    yield b
    b.close()


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlPlane]:
    c = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    c.init_schema()
    yield c
    c.close()


@pytest.fixture
def project(control: ControlPlane) -> str:
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    return "p1"


# --------------------------------------------------------------------------- #
# Schema + severity ladder
# --------------------------------------------------------------------------- #


def test_severity_ladder_and_ge() -> None:
    assert SEVERITY_RANK["info"] < SEVERITY_RANK["notice"]
    assert SEVERITY_RANK["notice"] < SEVERITY_RANK["warning"]
    assert SEVERITY_RANK["warning"] < SEVERITY_RANK["critical"]
    assert severity_ge("warning", "info")
    assert severity_ge("critical", "critical")
    assert not severity_ge("info", "warning")


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def test_phi_returns_half_at_zero() -> None:
    assert abs(_phi(0.0) - 0.5) < 1e-9
    assert _phi(5.0) > 0.999_99
    assert _phi(-5.0) < 1e-5


def test_severity_from_z_thresholds() -> None:
    assert _severity_from_z(0.5) == "info"
    assert _severity_from_z(3.0) == "notice"
    assert _severity_from_z(4.0) == "warning"
    assert _severity_from_z(5.5) == "critical"


def test_ks_two_sided_detects_clear_shift() -> None:
    import random

    rng = random.Random(0)
    baseline = [rng.gauss(0.0, 1.0) for _ in range(200)]
    sample = [rng.gauss(2.0, 1.0) for _ in range(100)]
    d, p = _ks_two_sided(sample, baseline)
    assert d > 0.4
    assert p < 0.01


def test_ks_two_sided_returns_high_p_for_identical_samples() -> None:
    import random

    rng = random.Random(1)
    samples = [rng.gauss(0.0, 1.0) for _ in range(200)]
    # Same generator → same distribution; KS p should be high.
    other = [rng.gauss(0.0, 1.0) for _ in range(200)]
    _, p = _ks_two_sided(samples, other)
    assert p > 0.05


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #


def test_rolling_mean_zscore_detects_two_sided_spike() -> None:
    """A clear positive spike against a varied baseline triggers a detection."""
    import random

    rng = random.Random(0)
    baseline = [rng.gauss(1.0, 0.1) for _ in range(40)]
    series = baseline + [5.0]
    detector = RollingMeanZScore(z_threshold=3.0)
    detections = detector(signal_name="error_rate", series=series)
    assert len(detections) == 1
    det = detections[0]
    assert det.kind == "trend_shift"
    assert det.severity in ("notice", "warning", "critical")
    assert det.current_value > det.baseline_value
    # The metadata `z` is a signed-float string; verify it parses to >0.
    z_value = float(det.metadata.get("z", "0"))
    assert z_value > 3.0
    assert det.metadata.get("baseline_n") == "40"


def test_rolling_mean_zscore_quiet_on_no_change() -> None:
    """Series with no spike → no detection."""
    import random

    rng = random.Random(0)
    series = [rng.gauss(1.0, 0.1) for _ in range(40)]
    detector = RollingMeanZScore(z_threshold=3.0)
    assert detector(signal_name="x", series=series) == []


def test_rolling_mean_zscore_zero_sd_skips_detection() -> None:
    """All-constant baseline → SD = 0 → detector skips."""
    series = [1.0] * 20 + [5.0]
    detector = RollingMeanZScore()
    assert detector(signal_name="x", series=series) == []


def test_western_electric_rule_1_fires() -> None:
    """Single point > 3σ from mean triggers rule 1."""
    import random

    rng = random.Random(2)
    series = [rng.gauss(0.0, 1.0) for _ in range(15)] + [10.0]
    detections = WesternElectricRules()(signal_name="cost", series=series)
    kinds = [d.metadata.get("rule") for d in detections]
    assert "1" in kinds
    rule1 = next(d for d in detections if d.metadata.get("rule") == "1")
    assert rule1.severity == "critical"
    assert rule1.kind == "threshold_breach_implicit"


def test_western_electric_rule_4_fires_on_eight_same_side() -> None:
    """8 consecutive points on the same side of the mean → rule 4."""
    # Baseline mean ≈ 0; tail = 8 positives.
    series = [-1.0, -0.5, -1.5, -0.5, 0.5, 1.0, 1.5, 0.5, 0.5, 0.7, 0.6, 0.8, 0.9, 1.0, 1.1, 1.2]
    detections = WesternElectricRules()(signal_name="x", series=series)
    rules = {d.metadata.get("rule") for d in detections}
    assert "4" in rules


def test_western_electric_quiet_on_noise() -> None:
    import random

    rng = random.Random(3)
    series = [rng.gauss(0.0, 1.0) for _ in range(20)]
    # No rule should consistently fire on random noise; tolerate up to 1 hit
    # in case of unlucky run-length.
    detections = WesternElectricRules()(signal_name="x", series=series)
    assert len(detections) <= 1


def test_ks_distance_detects_drift() -> None:
    import random

    rng = random.Random(5)
    series = [rng.gauss(0.0, 1.0) for _ in range(100)] + [
        rng.gauss(3.0, 1.0) for _ in range(60)
    ]
    detector = KSDistance(current_n=60, p_threshold=0.01)
    detections = detector(signal_name="latency", series=series)
    assert len(detections) == 1
    det = detections[0]
    assert det.kind == "distribution_drift"
    assert det.current_value > det.baseline_value
    assert float(det.metadata["p_value"]) < 0.01


def test_ks_distance_quiet_on_identical_distribution() -> None:
    import random

    rng = random.Random(6)
    series = [rng.gauss(0.0, 1.0) for _ in range(200)]
    assert KSDistance(current_n=50)(signal_name="x", series=series) == []


def test_calibration_drift_detects_growing_rmse() -> None:
    # Residuals: small for the first 25, larger for the last 25.
    base = [0.05, -0.04, 0.06, -0.03, 0.05] * 5
    bad = [0.5, -0.6, 0.7, -0.5, 0.6] * 5
    residuals = base + bad
    detector = CalibrationDrift(current_n=25, rmse_multiplier=2.0)
    detections = detector(signal_name="judge.quality", series=residuals)
    assert len(detections) == 1
    det = detections[0]
    assert det.kind == "calibration_drift"
    assert det.current_value > det.baseline_value
    assert det.severity in ("warning", "critical")


def test_calibration_drift_quiet_on_stable_residuals() -> None:
    series = [0.05, -0.05] * 30
    assert CalibrationDrift()(signal_name="x", series=series) == []


# --------------------------------------------------------------------------- #
# Control-plane CRUD
# --------------------------------------------------------------------------- #


def test_record_insight_round_trip(control: ControlPlane, project: str) -> None:
    row = control.record_insight(
        project_id=project,
        kind="trend_shift",
        severity="warning",
        signal_name="error_rate",
        baseline_value=0.01,
        current_value=0.12,
        observed_at=_NOW.isoformat(),
        evidence_sql="SELECT 1",
        confidence=0.9,
        summary="Spike",
        metadata={"k": "v"},
    )
    fetched = control.get_insight(project_id=project, insight_id=row["id"])
    assert fetched["signal_name"] == "error_rate"
    assert fetched["severity"] == "warning"
    assert fetched["current_value"] == pytest.approx(0.12)


def test_record_insight_rejects_bad_severity(
    control: ControlPlane, project: str
) -> None:
    with pytest.raises(ValueError, match="invalid severity"):
        control.record_insight(
            project_id=project,
            kind="trend_shift",
            severity="bogus",
            signal_name="x",
            baseline_value=0.0,
            current_value=0.0,
            observed_at=_NOW.isoformat(),
            evidence_sql="",
            confidence=1.0,
        )


def test_list_insights_filters_by_severity_and_since(
    control: ControlPlane, project: str
) -> None:
    for sev in ("info", "warning", "critical"):
        control.record_insight(
            project_id=project,
            kind="trend_shift",
            severity=sev,
            signal_name="x",
            baseline_value=0.0,
            current_value=1.0,
            observed_at=_NOW.isoformat(),
            evidence_sql="",
            confidence=0.5,
        )
    all_rows = control.list_insights(project_id=project)
    assert len(all_rows) == 3
    warn_plus = control.list_insights(project_id=project, min_severity="warning")
    assert {r["severity"] for r in warn_plus} == {"warning", "critical"}
    # since filter
    long_ago = (_NOW - timedelta(days=999)).isoformat()
    assert len(control.list_insights(project_id=project, since=long_ago)) == 3
    future = (_NOW + timedelta(days=999)).isoformat()
    assert control.list_insights(project_id=project, since=future) == []
    # bad min_severity rejected
    with pytest.raises(ValueError, match="invalid min_severity"):
        control.list_insights(project_id=project, min_severity="bogus")


def test_already_seen_insight(control: ControlPlane, project: str) -> None:
    control.record_insight(
        project_id=project,
        kind="trend_shift",
        severity="info",
        signal_name="x",
        baseline_value=0.0,
        current_value=1.0,
        observed_at="2026-06-04T12:00:00+00:00",
        evidence_sql="",
        confidence=0.5,
    )
    assert control.already_seen_insight(
        project_id=project, signal_name="x", kind="trend_shift",
        observed_day="2026-06-04",
    )
    # Different day → not seen.
    assert not control.already_seen_insight(
        project_id=project, signal_name="x", kind="trend_shift",
        observed_day="2026-06-05",
    )
    # Different signal → not seen.
    assert not control.already_seen_insight(
        project_id=project, signal_name="y", kind="trend_shift",
        observed_day="2026-06-04",
    )


def test_get_insight_missing_raises_keyerror(
    control: ControlPlane, project: str
) -> None:
    with pytest.raises(KeyError):
        control.get_insight(project_id=project, insight_id="ins_nope")


# --------------------------------------------------------------------------- #
# Engine round-trip
# --------------------------------------------------------------------------- #


def _seed_events(backend: DuckDBBackend, project_id: str, spike: bool) -> None:
    """Write 30 hourly events; if ``spike`` make the most recent one fail."""
    obs: list[Observation] = []
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    for h in range(30):
        start = now - timedelta(hours=29 - h)
        is_error = spike and h == 29
        obs.append(
            Observation(
                project_id=project_id,
                trace_id=f"t-{h}",
                observation_id=f"o-{h}",
                name="gen",
                type="GENERATION",
                start_time=start,
                end_time=start + timedelta(milliseconds=50),
                duration_ms=50,
                ingested_at=start,
                status="error" if is_error else "ok",
                usage=TokenUsage(total_tokens=100),
                cost=CostBreakdown(total_cost_usd=0.5 if h == 29 else 0.01),
                event_version=1,
            )
        )
    backend.write_events(obs)


def test_engine_persists_only_above_min_severity(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    """An info-level detection is dropped when min_persist_severity=warning."""
    seen: list[Detection] = []

    class FakeDetector:
        name = "fake"

        def __call__(self, *, signal_name, series, baseline=None):
            seen.append(...)  # type: ignore[arg-type]
            return [
                Detection(
                    kind="trend_shift",
                    severity="info",
                    signal_name=signal_name,
                    baseline_value=0.0,
                    current_value=1.0,
                    confidence=0.5,
                    summary="info-level",
                )
            ]

    probe = SignalProbe(
        name="error_rate_per_hour",
        sql="SELECT count() AS value FROM events_current",
    )
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[FakeDetector()],
        probes=[probe],
        min_persist_severity="warning",
    )
    _seed_events(backend, project, spike=True)
    persisted = engine.evaluate(project_id=project)
    assert persisted == []
    assert control.list_insights(project_id=project) == []


def test_engine_dedups_within_a_day(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    """Two ticks within the same day → one persisted row per (signal, kind)."""

    class HotDetector:
        name = "hot"

        def __call__(self, *, signal_name, series, baseline=None):
            return [
                Detection(
                    kind="trend_shift",
                    severity="warning",
                    signal_name=signal_name,
                    baseline_value=0.0,
                    current_value=1.0,
                    confidence=0.95,
                    summary="hot",
                )
            ]

    probe = SignalProbe(
        name="error_rate_per_hour",
        sql="SELECT 1.0 AS value",
    )
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[HotDetector()],
        probes=[probe],
    )
    _seed_events(backend, project, spike=False)
    engine.evaluate(project_id=project, now=_NOW)
    engine.evaluate(project_id=project, now=_NOW + timedelta(minutes=10))
    rows = control.list_insights(project_id=project)
    assert len(rows) == 1


def test_engine_records_evidence_sql_from_probe(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    """Insight's evidence_sql is the probe's SQL unless the detector supplied one."""

    class TaggedDetector:
        name = "tagged"

        def __call__(self, *, signal_name, series, baseline=None):
            return [
                Detection(
                    kind="trend_shift",
                    severity="warning",
                    signal_name=signal_name,
                    baseline_value=0.0,
                    current_value=1.0,
                    confidence=0.9,
                    summary="ok",
                )
            ]

    probe = SignalProbe(
        name="signal_x", sql="SELECT 0.5 AS value /* tagged */"
    )
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[TaggedDetector()],
        probes=[probe],
    )
    engine.evaluate(project_id=project, now=_NOW)
    rows = control.list_insights(project_id=project)
    assert rows
    assert "tagged" in rows[0]["evidence_sql"]


def test_engine_continues_when_a_probe_or_detector_errors(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    """Failures in one detector / probe don't abort the rest of the pass."""

    class BoomDetector:
        name = "boom"

        def __call__(self, *, signal_name, series, baseline=None):
            raise RuntimeError("intentional")

    class OKDetector:
        name = "ok"

        def __call__(self, *, signal_name, series, baseline=None):
            return [
                Detection(
                    kind="trend_shift",
                    severity="warning",
                    signal_name=signal_name,
                    baseline_value=0.0,
                    current_value=1.0,
                    confidence=0.9,
                    summary="ok",
                )
            ]

    bad_probe = SignalProbe(name="bad", sql="SELECT 1 -- not allowed by readonly")
    good_probe = SignalProbe(name="ok_signal", sql="SELECT 0.5 AS value")
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[BoomDetector(), OKDetector()],
        probes=[bad_probe, good_probe],
    )
    persisted = engine.evaluate(project_id=project, now=_NOW)
    # OKDetector against good_probe should still land.
    assert any(p["signal_name"] == "ok_signal" for p in persisted)


# --------------------------------------------------------------------------- #
# ReplayVerifiedScanner — Q-20 follow-up
# --------------------------------------------------------------------------- #


def test_replay_verified_scanner_lifts_stage2_edges(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    """Stage-2 COUNTERFACTUAL_REPLAY edges → decisive_error_verified_by_replay insights."""
    # Write a Stage-2 edge directly so we don't need a full pipeline run.
    backend.write_causal_edges(
        [
            CausalEdge(
                project_id=project,
                trace_id="t-verified",
                source_observation_id="o-bad",
                target_observation_id="o-bad",
                edge_type="DECISIVE_ERROR",
                confidence=0.97,
                method="COUNTERFACTUAL_REPLAY",
                evidence="langgraph replay flipped failure to ok",
                replay_supported=True,
                computed_at=_NOW,
            )
        ]
    )
    scanner = ReplayVerifiedScanner()
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[],
        probes=[],
        replay_scanner=scanner,
    )
    persisted = engine.evaluate(project_id=project, now=_NOW + timedelta(minutes=1))
    assert len(persisted) == 1
    row = persisted[0]
    assert row["kind"] == "decisive_error_verified_by_replay"
    assert row["trace_id"] == "t-verified"
    assert row["severity"] == "critical"


def test_replay_verified_scanner_dedups_across_ticks(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    backend.write_causal_edges(
        [
            CausalEdge(
                project_id=project,
                trace_id="t-dedup",
                source_observation_id="o-x",
                target_observation_id="o-x",
                edge_type="DECISIVE_ERROR",
                confidence=0.97,
                method="COUNTERFACTUAL_REPLAY",
                evidence="ok",
                replay_supported=True,
                computed_at=_NOW,
            )
        ]
    )
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[],
        probes=[],
        replay_scanner=ReplayVerifiedScanner(),
    )
    engine.evaluate(project_id=project, now=_NOW)
    engine.evaluate(project_id=project, now=_NOW + timedelta(minutes=30))
    assert (
        len(control.list_insights(project_id=project))
        == 1
    )


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #


def test_anomaly_worker_starts_runs_stops(
    backend: DuckDBBackend, control: ControlPlane, project: str
) -> None:
    class HotDetector:
        name = "hot"

        def __call__(self, *, signal_name, series, baseline=None):
            return [
                Detection(
                    kind="trend_shift",
                    severity="warning",
                    signal_name=signal_name,
                    baseline_value=0.0,
                    current_value=1.0,
                    confidence=0.9,
                    summary="hot",
                )
            ]

    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[HotDetector()],
        probes=[SignalProbe(name="s", sql="SELECT 1.0 AS value")],
    )
    worker = AnomalyWorker(engine, project_id=project, interval_s=60)
    worker.start()
    try:
        # Worker is gated by interval_s, but the first iteration runs at start.
        for _ in range(60):
            if worker.last_count >= 1:
                break
            time.sleep(0.05)
        assert worker.last_count >= 1
    finally:
        worker.stop()


def test_default_detectors_contains_full_stack() -> None:
    names = {d.name for d in default_detectors()}
    assert names == {
        "rolling_mean_zscore",
        "western_electric",
        "ks_distance",
        "calibration_drift",
    }
