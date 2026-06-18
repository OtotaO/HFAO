"""AC §8 / §16 Q-19 acceptance tests — insight routing intelligence.

Covers:

  - Subscription schema + control-plane CRUD (upsert, get, list filters,
    delete, idempotent on the unique key, severity validation)
  - matcher: kind / signal-name glob / severity ladder
  - auto-resolved subscriber_id: ``auto:prompt_owner:<name>`` looks up the
    last ``create_prompt_version`` actor in the audit log
  - InsightRouter end-to-end: webhook fan-out for role/user kinds, agent
    dispatch for agent kind, delivery failures captured but never abort
    persistence
  - AnomalyEngine wires the router: persisted insights → router.route_insight
  - MonitorEngine wires the router: threshold breach → router.route_insight
  - matching_subscriptions() preview helper
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hfao.compute.anomaly import (
    AnomalyEngine,
    Detection,
    SignalProbe,
)
from hfao.compute.monitor import (
    MonitorEngine,
    create_monitor,
)
from hfao.compute.routing import (
    Delivery,
    InsightRouter,
    NoOpAgentDispatcher,
    RouteResult,
    matching_subscriptions,
)
from hfao.schema.events import CostBreakdown, Observation, TokenUsage
from hfao.schema.insights import SEVERITY_RANK
from hfao.schema.subscriptions import Subscription
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
def workspace_project(control: ControlPlane) -> tuple[str, str]:
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    return ws["id"], "p1"


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_subscription_round_trip_via_msgspec() -> None:
    import msgspec

    sub = Subscription(
        project_id="p1",
        id="sub_1",
        subscriber_kind="role",
        subscriber_id="role:owner",
        match_kind="*",
        match_signal_name="error_rate_*",
        match_min_severity="warning",
        channels=["https://hook.example/x"],
        created_at=_NOW,
        created_by="key_root",
    )
    encoded = msgspec.json.encode(sub)
    decoded = msgspec.json.decode(encoded, type=Subscription)
    assert decoded == sub


# --------------------------------------------------------------------------- #
# Control-plane CRUD
# --------------------------------------------------------------------------- #


def test_upsert_subscription_round_trip(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    row = control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:alice@example.com",
        channels=["https://hook.example/alerts"],
        match_kind="trend_shift",
        match_signal_name="error_rate_*",
        match_min_severity="warning",
    )
    fetched = control.get_subscription(
        project_id=project, subscription_id=row["id"]
    )
    assert fetched["subscriber_kind"] == "user"
    assert fetched["match_min_severity"] == "warning"


def test_upsert_subscription_is_idempotent_on_unique_key(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    """Same (project, kind, id, match_kind, match_signal_name) → upsert, not duplicate."""
    _, project = workspace_project
    a = control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:alice@example.com",
        channels=["https://hook.example/v1"],
        match_signal_name="error_rate_*",
        match_min_severity="info",
    )
    b = control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:alice@example.com",
        channels=["https://hook.example/v2"],
        match_signal_name="error_rate_*",
        match_min_severity="critical",
    )
    rows = control.list_subscriptions(project_id=project)
    assert len(rows) == 1
    assert b["match_min_severity"] == "critical"
    # Re-fetch by id to confirm channels updated.
    import json as _json

    channels = _json.loads(b["channels"])
    assert channels == ["https://hook.example/v2"]
    del a  # unused


def test_upsert_subscription_rejects_bad_kind_or_severity(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    with pytest.raises(ValueError, match="invalid subscriber_kind"):
        control.upsert_subscription(
            project_id=project,
            subscriber_kind="bogus",
            subscriber_id="x",
            channels=[],
        )
    with pytest.raises(ValueError, match="invalid match_min_severity"):
        control.upsert_subscription(
            project_id=project,
            subscriber_kind="user",
            subscriber_id="x",
            channels=[],
            match_min_severity="kinda-warning",
        )


def test_list_subscriptions_filters(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:alice@example.com",
        channels=[],
        match_kind="trend_shift",
        match_signal_name="error_rate_*",
    )
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:bob@example.com",
        channels=[],
        match_kind="*",
        match_signal_name="cost_*",
    )
    # Disabled row.
    sub = control.upsert_subscription(
        project_id=project,
        subscriber_kind="agent",
        subscriber_id="agent:bot",
        channels=[],
        enabled=False,
    )
    enabled = control.list_subscriptions(project_id=project, only_enabled=True)
    assert {r["subscriber_id"] for r in enabled} == {
        "user:alice@example.com",
        "user:bob@example.com",
    }
    only_trend = control.list_subscriptions(project_id=project, match_kind="trend_shift")
    assert any(r["subscriber_id"] == "user:alice@example.com" for r in only_trend)
    only_cost = control.list_subscriptions(
        project_id=project, match_signal_name="cost_per_hour"
    )
    assert any(r["subscriber_id"] == "user:bob@example.com" for r in only_cost)
    control.delete_subscription(project_id=project, subscription_id=sub["id"])
    assert all(r["id"] != sub["id"] for r in control.list_subscriptions(project_id=project))


def test_get_subscription_missing_raises_keyerror(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    with pytest.raises(KeyError):
        control.get_subscription(project_id=project, subscription_id="sub_nope")


# --------------------------------------------------------------------------- #
# Auto-resolved subscriber id
# --------------------------------------------------------------------------- #


def test_latest_audit_actor_returns_most_recent(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    workspace_id, project = workspace_project
    control.record_audit(
        workspace_id=workspace_id,
        actor="alice@example.com",
        action="create_prompt_version",
        target=f"{project}/greeter@v1",
    )
    control.record_audit(
        workspace_id=workspace_id,
        actor="bob@example.com",
        action="create_prompt_version",
        target=f"{project}/greeter@v2",
    )
    actor = control.latest_audit_actor(
        workspace_id=workspace_id,
        action="create_prompt_version",
        target_contains="/greeter@",
    )
    assert actor == "bob@example.com"


def test_latest_audit_actor_none_when_no_history(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    workspace_id, _ = workspace_project
    assert (
        control.latest_audit_actor(
            workspace_id=workspace_id,
            action="create_prompt_version",
            target_contains="/missing@",
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Router — matcher and dispatch
# --------------------------------------------------------------------------- #


class CapturingWebhook:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_for = fail_for or set()

    def __call__(self, url: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
        self.calls.append((url, payload))
        if url in self.fail_for:
            return False, "test-failure"
        return True, None


class CapturingAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_for: set[str] = set()

    def dispatch(
        self, *, agent_id: str, payload: dict[str, Any]
    ) -> tuple[bool, str | None]:
        self.calls.append((agent_id, payload))
        if agent_id in self.fail_for:
            return False, "agent-error"
        return True, None


def test_router_matches_by_kind_signal_severity(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:alice@example.com",
        channels=["https://hook.example/v1"],
        match_kind="trend_shift",
        match_signal_name="error_rate_*",
        match_min_severity="warning",
    )
    # Won't match: severity too low.
    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="info",
        payload={"summary": "x"},
    )
    assert res.deliveries == []
    assert webhook.calls == []
    # Will match: warning >= warning.
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
        payload={"summary": "x"},
    )
    assert len(res.deliveries) == 1
    assert webhook.calls
    assert webhook.calls[0][0] == "https://hook.example/v1"


def test_router_handles_wildcard_kind_and_signal(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="role",
        subscriber_id="role:owner",
        channels=["https://hook.example/any"],
        match_kind="*",
        match_signal_name="*",
        match_min_severity="info",
    )
    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    res = router.route_insight(
        project_id=project,
        kind="cost_anomaly",
        signal_name="cost_per_hour",
        severity="notice",
        payload={"k": "v"},
    )
    assert len(res.deliveries) == 1
    assert webhook.calls and webhook.calls[0][0] == "https://hook.example/any"


def test_router_glob_signal_name(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:cost-watcher@example.com",
        channels=["https://hook.example/cost"],
        match_signal_name="cost_*",
    )
    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    # cost_per_hour matches cost_*.
    res = router.route_insight(
        project_id=project,
        kind="cost_anomaly",
        signal_name="cost_per_hour",
        severity="warning",
        payload={},
    )
    assert len(res.deliveries) == 1
    # error_rate_per_hour does NOT match cost_*.
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
        payload={},
    )
    assert res.deliveries == []


def test_router_dispatches_agent_kind_via_agent_dispatcher(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="agent",
        subscriber_id="agent:on-call-bot",
        channels=[],
        match_min_severity="info",
    )
    agent = CapturingAgent()
    router = InsightRouter(
        control=control, webhook=CapturingWebhook(), agent_dispatcher=agent
    )
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
        payload={"summary": "spike"},
    )
    assert len(res.deliveries) == 1
    assert agent.calls
    agent_id, payload = agent.calls[0]
    assert agent_id == "agent:on-call-bot"
    assert payload["summary"] == "spike"


def test_router_records_failures_without_aborting(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:bad@example.com",
        channels=["https://hook.example/flaky", "https://hook.example/ok"],
        match_min_severity="info",
    )
    webhook = CapturingWebhook(fail_for={"https://hook.example/flaky"})
    router = InsightRouter(control=control, webhook=webhook)
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
        payload={},
    )
    assert len(res.webhook_failures) == 1
    # Both URLs were attempted.
    assert {c[0] for c in webhook.calls} == {
        "https://hook.example/flaky",
        "https://hook.example/ok",
    }


def test_router_resolves_auto_prompt_owner(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    workspace_id, project = workspace_project
    control.record_audit(
        workspace_id=workspace_id,
        actor="alice@example.com",
        action="create_prompt_version",
        target=f"{project}/greeter@v1",
    )
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="auto:prompt_owner:greeter",
        channels=["https://hook.example/prompt-owner"],
    )
    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
        payload={"k": "v"},
        workspace_id=workspace_id,
    )
    assert len(res.deliveries) == 1
    assert res.deliveries[0].subscriber_id == "user:alice@example.com"
    assert webhook.calls


def test_router_skips_when_auto_subscriber_unresolved(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    workspace_id, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="auto:prompt_owner:never_existed",
        channels=["https://hook.example/x"],
    )
    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    res = router.route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
        payload={},
        workspace_id=workspace_id,
    )
    assert res.deliveries == []
    assert webhook.calls == []


def test_router_skips_disabled_subscriptions(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:offline@example.com",
        channels=["https://hook.example/x"],
        enabled=False,
    )
    webhook = CapturingWebhook()
    res = InsightRouter(control=control, webhook=webhook).route_insight(
        project_id=project,
        kind="trend_shift",
        signal_name="any",
        severity="critical",
        payload={},
    )
    assert res.deliveries == []
    assert webhook.calls == []


def test_matching_subscriptions_dry_run_helper(
    control: ControlPlane, workspace_project: tuple[str, str]
) -> None:
    _, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="role",
        subscriber_id="role:owner",
        channels=[],
        match_signal_name="error_rate_*",
        match_min_severity="warning",
    )
    matches = matching_subscriptions(
        control,
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="warning",
    )
    assert len(matches) == 1
    no_match = matching_subscriptions(
        control,
        project_id=project,
        kind="trend_shift",
        signal_name="error_rate_per_hour",
        severity="info",
    )
    assert no_match == []


# --------------------------------------------------------------------------- #
# Engine integration
# --------------------------------------------------------------------------- #


def _obs(
    *, obs_id: str, status: str = "ok", cost: float = 0.01, base_time: datetime = _NOW
) -> Observation:
    # ``base_time`` defaults to the fixed ``_NOW`` so the deterministic tests
    # (which assert against an explicit ``_NOW``-relative date range) keep their
    # behaviour. Tests that exercise a monitor with a wall-clock window
    # (``now() - INTERVAL …``) must pass a recent ``base_time`` so the seeded
    # events fall inside the window as real ``now()`` advances — otherwise the
    # event timestamps drift past the window from the fixed ``_NOW`` (time-bomb).
    now = base_time - timedelta(hours=1)
    return Observation(
        project_id="p1",
        trace_id=f"t-{obs_id}",
        observation_id=obs_id,
        name="gen",
        type="GENERATION",
        start_time=now,
        end_time=now + timedelta(milliseconds=50),
        duration_ms=50,
        ingested_at=now,
        status=status,  # type: ignore[arg-type]
        usage=TokenUsage(total_tokens=10),
        cost=CostBreakdown(total_cost_usd=cost),
        event_version=1,
    )


def test_anomaly_engine_routes_persisted_insights(
    backend: DuckDBBackend,
    control: ControlPlane,
    workspace_project: tuple[str, str],
) -> None:
    workspace_id, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:alice@example.com",
        channels=["https://hook.example/anomaly"],
        match_kind="trend_shift",
        match_signal_name="*",
        match_min_severity="warning",
    )

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

    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[HotDetector()],
        probes=[SignalProbe(name="error_rate_per_hour", sql="SELECT 1.0 AS value")],
        router=router,
        workspace_id=workspace_id,
    )
    backend.write_events([_obs(obs_id=str(i)) for i in range(3)])
    persisted = engine.evaluate(project_id=project, now=_NOW)
    assert persisted, "engine should have persisted at least one insight"
    assert webhook.calls, "router should have fanned out at least one webhook"
    url, payload = webhook.calls[0]
    assert url == "https://hook.example/anomaly"
    assert payload["kind"] == "trend_shift"
    assert payload["signal_name"] == "error_rate_per_hour"


def test_anomaly_engine_router_failure_does_not_block_persistence(
    backend: DuckDBBackend,
    control: ControlPlane,
    workspace_project: tuple[str, str],
) -> None:
    _, project = workspace_project

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

    class BoomRouter:
        def route_insight(self, **_kwargs):
            raise RuntimeError("router exploded")

    engine = AnomalyEngine(
        backend=backend,
        control=control,
        detectors=[HotDetector()],
        probes=[SignalProbe(name="x", sql="SELECT 1.0 AS value")],
        router=BoomRouter(),
    )
    persisted = engine.evaluate(project_id=project, now=_NOW)
    assert persisted, "insight should still land even when router raises"


def test_monitor_engine_routes_alert_on_breach(
    backend: DuckDBBackend,
    control: ControlPlane,
    workspace_project: tuple[str, str],
) -> None:
    workspace_id, project = workspace_project
    control.upsert_subscription(
        project_id=project,
        subscriber_kind="user",
        subscriber_id="user:on-call@example.com",
        channels=["https://hook.example/monitor"],
        match_kind="threshold_breach_implicit",
        match_signal_name="*",
        match_min_severity="info",
    )
    # Two error events + one ok → error rate breach. The monitor below uses a
    # wall-clock "7d" window, so anchor the seeded events to a recent base_time
    # (not the fixed _NOW, which drifts out of the window as real now() moves).
    recent = datetime.now(timezone.utc)
    backend.write_events(
        [
            _obs(obs_id="e1", status="error", base_time=recent),
            _obs(obs_id="e2", status="error", base_time=recent),
            _obs(obs_id="ok1", status="ok", base_time=recent),
        ]
    )
    monitor = create_monitor(
        control,
        project_id=project,
        name="error_rate_high",
        nl_description="error rate over the last week",
        threshold=0.1,
        operator="gt",
        window="7d",
        channels=["https://hook.example/direct-channel"],
    )
    webhook = CapturingWebhook()
    router = InsightRouter(control=control, webhook=webhook)
    # Use the same webhook for the engine's own dispatch *and* for routing
    # so we can see both calls in one capture list.
    engine = MonitorEngine(
        backend,
        control,
        webhook=webhook,
        router=router,
        workspace_id=workspace_id,
    )
    evaluation = engine.evaluate(monitor)
    assert evaluation.threshold_breached
    urls = {c[0] for c in webhook.calls}
    # Direct monitor channel + the routed subscription channel both fired.
    assert "https://hook.example/direct-channel" in urls
    assert "https://hook.example/monitor" in urls


def test_routeresult_default_collections_isolated() -> None:
    """RouteResult dataclass defaults must not share mutable state."""
    r1 = RouteResult()
    r2 = RouteResult()
    assert r1.deliveries is not r2.deliveries
    r1.deliveries.append(
        Delivery(subscription_id="s", subscriber_kind="user", subscriber_id="u")
    )
    assert r2.deliveries == []


def test_no_op_agent_dispatcher_returns_success() -> None:
    ok, err = NoOpAgentDispatcher().dispatch(agent_id="agent:x", payload={"k": "v"})
    assert ok is True
    assert err is None


def test_severity_rank_ordering_invariant() -> None:
    """Q-19's severity gate relies on the SEVERITY_RANK ladder."""
    assert (
        SEVERITY_RANK["info"]
        < SEVERITY_RANK["notice"]
        < SEVERITY_RANK["warning"]
        < SEVERITY_RANK["critical"]
    )
