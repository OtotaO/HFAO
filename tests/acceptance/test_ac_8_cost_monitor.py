"""AC §8 acceptance tests — cost + monitor subset.

Covers the §8.5 lines that pertain to the cost rollup engine and the monitor
engine:

    - test_cost_rollup_pivot_by_user_and_model
    - test_monitor_nl_to_sql_generation
    - test_monitor_fires_on_threshold

Plus per-piece coverage of the keyword-template generator, alert dispatch,
webhook delivery error capture, idempotent refresh, and the
``CostRollupWorker`` / ``MonitorWorker`` lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hfao.compute.cost import (
    CostRollupWorker,
    staleness,
)
from hfao.compute.cost import (
    refresh as refresh_cost,
)
from hfao.compute.monitor import (
    KeywordTemplateGenerator,
    MonitorEngine,
    create_monitor,
    parse_window_seconds,
)
from hfao.schema.events import CostBreakdown, Observation, TokenUsage
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def _obs(
    *,
    obs_id: str,
    user_id: str = "alice",
    model: str = "claude-haiku-4-5",
    cost: float = 0.01,
    tokens: int = 100,
    status: str = "ok",
    start_offset_ms: int = 0,
    duration_ms: int = 50,
    base_time: datetime = _NOW,
) -> Observation:
    # ``base_time`` defaults to the fixed ``_NOW`` so the deterministic cost
    # tests (which assert against an explicit ``_NOW``-relative date range) are
    # unchanged. Tests that exercise the monitor engine's wall-clock window
    # (``now() - INTERVAL …``) must pass a recent ``base_time`` so the seeded
    # events fall inside the window regardless of the calendar date the suite
    # runs on — otherwise the test silently time-bombs once real ``now()``
    # drifts past the window from the fixed ``_NOW``.
    start = base_time + timedelta(milliseconds=start_offset_ms)
    return Observation(
        project_id="p1",
        trace_id=f"t-{obs_id}",
        observation_id=obs_id,
        name="generate",
        type="GENERATION",
        start_time=start,
        end_time=start + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
        ingested_at=start,
        status=status,  # type: ignore[arg-type]
        user_id=user_id,
        model=model,
        usage=TokenUsage(total_tokens=tokens),
        cost=CostBreakdown(total_cost_usd=cost),
        event_version=1,
    )


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


# --------------------------------------------------------------------------- #
# Cost rollup
# --------------------------------------------------------------------------- #


def test_cost_rollup_pivot_by_user_and_model(backend: DuckDBBackend) -> None:
    """§8.5 line: a refreshed cost_daily pivots by (user_id, model) correctly."""
    backend.write_events(
        [
            _obs(obs_id="o1", user_id="alice", model="gpt-4o", cost=0.02, tokens=200),
            _obs(obs_id="o2", user_id="alice", model="gpt-4o", cost=0.03, tokens=300),
            _obs(
                obs_id="o3",
                user_id="alice",
                model="claude-haiku-4-5",
                cost=0.01,
                tokens=100,
            ),
            _obs(obs_id="o4", user_id="bob", model="gpt-4o", cost=0.04, tokens=400),
        ]
    )
    result = refresh_cost(backend)
    # 3 distinct (user, model) groups for one day.
    assert result.row_count == 3

    rows = backend.cost_rollup(
        "p1",
        date_from=_NOW - timedelta(days=1),
        date_to=_NOW + timedelta(days=1),
        group_by=["user_id", "model"],
    )
    pivot = {(r["user_id"], r["model"]): r for r in rows}
    assert pytest.approx(pivot[("alice", "gpt-4o")]["total_cost_usd"], rel=1e-6) == 0.05
    assert pivot[("alice", "gpt-4o")]["total_tokens"] == 500
    assert pivot[("alice", "gpt-4o")]["call_count"] == 2
    assert pivot[("bob", "gpt-4o")]["call_count"] == 1
    assert pytest.approx(pivot[("alice", "claude-haiku-4-5")]["total_cost_usd"]) == 0.01


def test_cost_refresh_is_idempotent(backend: DuckDBBackend) -> None:
    """Re-refreshing twice should not double-count anything."""
    backend.write_events([_obs(obs_id=f"o{i}", cost=0.01, tokens=10) for i in range(3)])
    first = refresh_cost(backend).row_count
    second = refresh_cost(backend).row_count
    assert first == second == 1  # all same group


def test_cost_refresh_staleness() -> None:
    """`staleness(None) is None`; after refresh, less than 5 seconds."""
    assert staleness(None) is None


def test_cost_rollup_worker_lifecycle(backend: DuckDBBackend) -> None:
    """The worker can be started and stopped; running the loop refreshes once."""
    backend.write_events([_obs(obs_id="o1", cost=0.05, tokens=50)])
    worker = CostRollupWorker(backend, interval_s=1)
    worker.start()
    try:
        # Wait at most 3s for the first iteration.
        import time

        for _ in range(60):
            if worker.last_result is not None:
                break
            time.sleep(0.05)
        assert worker.last_result is not None
        assert worker.last_result.row_count >= 1
    finally:
        worker.stop()


# --------------------------------------------------------------------------- #
# Monitor — NL → SQL generator
# --------------------------------------------------------------------------- #


def test_monitor_nl_to_sql_generation() -> None:
    """§8.5 line: the keyword-template generator produces canonical SQL."""
    gen = KeywordTemplateGenerator()
    error_rate = gen.generate(description="error rate over the last hour", window="1h")
    assert "error" in error_rate.sql.lower()
    assert "events_current" in error_rate.sql
    assert "now() - INTERVAL '1 HOUR'" in error_rate.sql
    assert error_rate.matched_template == "error_rate"

    cost = gen.generate(description="total cost", window="24h")
    assert "total_cost_usd" in cost.sql
    assert "24 HOUR" in cost.sql

    latency = gen.generate(description="latency p95", window="5m")
    assert "quantile_cont" in latency.sql
    assert "5 MINUTE" in latency.sql

    tokens = gen.generate(description="token usage", window="1d")
    assert "total_tokens" in tokens.sql
    assert "1 DAY" in tokens.sql

    default = gen.generate(description="anything else", window="1h")
    assert "count()" in default.sql
    assert default.matched_template == "default_count"


def test_parse_window_seconds() -> None:
    assert parse_window_seconds("5m") == 300
    assert parse_window_seconds("1h") == 3600
    assert parse_window_seconds("24h") == 86_400
    assert parse_window_seconds("1d") == 86_400
    assert parse_window_seconds("1w") == 604_800
    # Garbage falls back to 1 hour.
    assert parse_window_seconds("not-a-window") == 3600


def test_monitor_invalid_window_falls_back() -> None:
    sql = KeywordTemplateGenerator().generate(description="error rate", window="garbage").sql
    assert "1 HOUR" in sql  # safe default


# --------------------------------------------------------------------------- #
# Monitor — engine + alert dispatch
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded_project(control: ControlPlane) -> str:
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(project_id="p1", workspace_id=ws["id"], slug="p1", name="p1")
    return "p1"


class CapturingWebhook:
    """Test double for the engine's webhook dispatcher."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_for = fail_for or set()

    def __call__(self, url: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
        self.calls.append((url, payload))
        if url in self.fail_for:
            return False, "test-failure"
        return True, None


def test_monitor_fires_on_threshold(
    backend: DuckDBBackend,
    control: ControlPlane,
    seeded_project: str,
) -> None:
    """§8.5 line: a threshold breach triggers an alert + webhook delivery."""
    # Seed two error events and three ok events → error_rate = 0.4.
    # Anchor to a recent time so they fall inside the monitor's wall-clock
    # "7d" window (engine uses SQL now()), independent of the run date.
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    obs_seq: list[Observation] = []
    for i in range(2):
        obs_seq.append(
            _obs(
                obs_id=f"err{i}",
                status="error",
                start_offset_ms=i,
                cost=0.01,
                base_time=recent,
            )
        )
    for i in range(3):
        obs_seq.append(
            _obs(
                obs_id=f"ok{i}",
                status="ok",
                start_offset_ms=10 + i,
                cost=0.01,
                base_time=recent,
            )
        )
    backend.write_events(obs_seq)

    monitor = create_monitor(
        control,
        project_id=seeded_project,
        name="error_rate_high",
        nl_description="error rate over the last week",
        threshold=0.10,
        operator="gt",
        window="7d",
        channels=["http://alerts.local/webhook"],
    )

    webhook = CapturingWebhook()
    engine = MonitorEngine(backend, control, webhook=webhook)
    evaluation = engine.evaluate(monitor)

    assert evaluation.threshold_breached is True
    assert evaluation.actual_value == pytest.approx(0.4, rel=1e-6)
    assert evaluation.alert is not None
    # Webhook was hit with a structured payload.
    assert len(webhook.calls) == 1
    url, payload = webhook.calls[0]
    assert url == "http://alerts.local/webhook"
    assert payload["actual_value"] == pytest.approx(0.4, rel=1e-6)
    assert payload["threshold"] == 0.10
    assert payload["monitor_name"] == "error_rate_high"
    # Alert recorded in the control plane.
    alerts = control.list_alerts(project_id=seeded_project)
    assert len(alerts) == 1
    assert alerts[0]["actual_value"] == pytest.approx(0.4, rel=1e-6)


def test_monitor_does_not_fire_when_below_threshold(
    backend: DuckDBBackend, control: ControlPlane, seeded_project: str
) -> None:
    """When the value stays under threshold, no alert + no webhook call."""
    backend.write_events([_obs(obs_id=f"o{i}", status="ok", start_offset_ms=i) for i in range(4)])
    monitor = create_monitor(
        control,
        project_id=seeded_project,
        name="error_rate_under",
        nl_description="error rate over the last week",
        threshold=0.5,
        operator="gt",
        window="7d",
        channels=["http://alerts.local/never-called"],
    )
    webhook = CapturingWebhook()
    engine = MonitorEngine(backend, control, webhook=webhook)
    evaluation = engine.evaluate(monitor)
    assert evaluation.threshold_breached is False
    assert evaluation.alert is None
    assert webhook.calls == []
    assert control.list_alerts(project_id=seeded_project) == []


def test_monitor_records_delivery_errors(
    backend: DuckDBBackend, control: ControlPlane, seeded_project: str
) -> None:
    """A breach with a failing channel records the error but still persists alert."""
    # Recent base_time so events fall inside the monitor's wall-clock window.
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    backend.write_events(
        [
            _obs(obs_id="e1", status="error", base_time=recent),
            _obs(obs_id="e2", status="error", start_offset_ms=1, base_time=recent),
        ]
    )
    monitor = create_monitor(
        control,
        project_id=seeded_project,
        name="cost_high",
        nl_description="total cost",
        threshold=0.0,
        operator="gt",
        window="7d",
        channels=[
            "http://primary.local/hook",
            "http://flaky.local/hook",
        ],
    )
    webhook = CapturingWebhook(fail_for={"http://flaky.local/hook"})
    engine = MonitorEngine(backend, control, webhook=webhook)
    evaluation = engine.evaluate(monitor)
    assert evaluation.threshold_breached is True
    alert_row = control.list_alerts(project_id=seeded_project)[0]
    import json as _json

    delivered = _json.loads(alert_row["channels_notified"])
    errors = _json.loads(alert_row["delivery_errors"])
    assert delivered == ["http://primary.local/hook"]
    assert any("flaky.local" in e for e in errors)


def test_monitor_evaluate_all_enabled(
    backend: DuckDBBackend, control: ControlPlane, seeded_project: str
) -> None:
    """Disabled monitors are skipped; enabled ones run."""
    backend.write_events(
        [_obs(obs_id="e1", status="error"), _obs(obs_id="e2", status="error", start_offset_ms=1)]
    )
    create_monitor(
        control,
        project_id=seeded_project,
        name="enabled_one",
        nl_description="error rate",
        threshold=0.01,
        operator="gt",
        window="7d",
    )
    disabled = create_monitor(
        control,
        project_id=seeded_project,
        name="disabled_one",
        nl_description="error rate",
        threshold=0.01,
        operator="gt",
        window="7d",
    )
    control.set_monitor_enabled(project_id=seeded_project, monitor_id=disabled["id"], enabled=False)
    engine = MonitorEngine(backend, control, webhook=CapturingWebhook())
    results = engine.evaluate_all_enabled(project_id=seeded_project)
    assert len(results) == 1
    assert results[0].monitor_id != disabled["id"]


def test_monitor_create_persists_frozen_sql(control: ControlPlane, seeded_project: str) -> None:
    """The SQL on the persisted monitor row matches the generator's output."""
    monitor = create_monitor(
        control,
        project_id=seeded_project,
        name="frozen",
        nl_description="error rate",
        threshold=0.5,
        operator="gt",
        window="1h",
    )
    # Persisted SQL matches the keyword template.
    persisted = control.get_monitor(project_id=seeded_project, monitor_id=monitor["id"])
    expected = KeywordTemplateGenerator().generate(description="error rate", window="1h").sql
    assert persisted["sql_query"] == expected


def test_monitor_rejects_invalid_operator(control: ControlPlane, seeded_project: str) -> None:
    with pytest.raises(ValueError, match="invalid operator"):
        create_monitor(
            control,
            project_id=seeded_project,
            name="bad",
            nl_description="error rate",
            threshold=0.0,
            operator="approximately",
            window="1h",
        )


def test_monitor_engine_marks_last_evaluated(
    backend: DuckDBBackend, control: ControlPlane, seeded_project: str
) -> None:
    backend.write_events([_obs(obs_id="o1", status="ok")])
    monitor = create_monitor(
        control,
        project_id=seeded_project,
        name="touch",
        nl_description="error rate",
        threshold=99.0,
        operator="gt",
        window="1h",
    )
    engine = MonitorEngine(backend, control, webhook=CapturingWebhook())
    engine.evaluate(monitor)
    refreshed = control.get_monitor(project_id=seeded_project, monitor_id=monitor["id"])
    assert refreshed["last_evaluated_at"] is not None
