"""AC §6 retention acceptance tests (SPEC §6.4 / §6.7).

Covers:

    - test_retention_purges_old_rows   — the §6.7 line
    - test_retention_skips_disabled_projects
    - test_retention_respects_per_table_timestamp_column
    - test_retention_purges_body_offload_files
    - test_retention_worker_lifecycle
    - test_retention_policy_crud_round_trip
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hfao.compute.retention import RetentionWorker, run_once
from hfao.schema.causal import CausalEdge
from hfao.schema.events import CostBreakdown, Observation, TokenUsage
from hfao.schema.scores import Score
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend


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


def _obs(
    *,
    observation_id: str,
    project_id: str = "p1",
    start_offset_days: int = 0,
    status: str = "ok",
) -> Observation:
    start = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc) - timedelta(
        days=start_offset_days
    )
    return Observation(
        project_id=project_id,
        trace_id=f"t-{observation_id}",
        observation_id=observation_id,
        name="step",
        type="GENERATION",
        start_time=start,
        end_time=start + timedelta(milliseconds=100),
        duration_ms=100,
        ingested_at=start,
        status=status,  # type: ignore[arg-type]
        usage=TokenUsage(total_tokens=10),
        cost=CostBreakdown(),
        event_version=1,
    )


# --------------------------------------------------------------------------- #


def test_retention_policy_crud_round_trip(control: ControlPlane) -> None:
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    # Default fetch creates a row with defaults.
    policy = control.get_retention_policy(project_id="p1")
    assert policy["hot_days"] == 30
    assert policy["warm_days"] == 365
    assert policy["bodies_days"] == 90
    assert policy["enabled"] in (1, True)
    # Override.
    updated = control.upsert_retention_policy(
        project_id="p1", hot_days=7, warm_days=30, bodies_days=14, enabled=False
    )
    assert updated["hot_days"] == 7
    assert updated["enabled"] in (0, False)
    # list_retention_policies sees it.
    assert any(p["project_id"] == "p1" for p in control.list_retention_policies())
    # Negative day-counts rejected.
    with pytest.raises(ValueError, match="must be ≥ 0"):
        control.upsert_retention_policy(project_id="p1", hot_days=-1)


def test_retention_purges_old_rows(
    backend: DuckDBBackend, control: ControlPlane
) -> None:
    """§6.7 line: rows older than ``hot_days`` are deleted."""
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    control.upsert_retention_policy(project_id="p1", hot_days=10)

    # Old event (40 days ago) + fresh event (today).
    backend.write_events(
        [
            _obs(observation_id="old1", start_offset_days=40),
            _obs(observation_id="fresh1", start_offset_days=1),
        ]
    )

    # Anchor "now" so the test is deterministic.
    anchor = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    result = run_once(backend, control, now=anchor)

    counts = result.per_project["p1"]
    assert counts["events"] == 1  # old1 deleted
    # The fresh row survives.
    surviving = backend.list_traces("p1", limit=100)
    assert {t["trace_id"] for t in surviving} == {"t-fresh1"}


def test_retention_purges_scores_and_causal_edges(
    backend: DuckDBBackend, control: ControlPlane
) -> None:
    """Scores and causal edges are pruned on their own timestamp columns."""
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    control.upsert_retention_policy(project_id="p1", hot_days=10)

    anchor = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    backend.write_events([_obs(observation_id="x", start_offset_days=1)])
    backend.write_scores(
        [
            Score(
                project_id="p1",
                trace_id="t-x",
                name="quality",
                value=0.9,
                source="ANNOTATION",
                timestamp=anchor - timedelta(days=40),  # old
            ),
            Score(
                project_id="p1",
                trace_id="t-x",
                name="quality",
                value=0.8,
                source="ANNOTATION",
                timestamp=anchor - timedelta(days=2),   # fresh
            ),
        ]
    )
    backend.write_causal_edges(
        [
            CausalEdge(
                project_id="p1",
                trace_id="t-x",
                source_observation_id="a",
                target_observation_id="b",
                edge_type="HANDOFF",
                confidence=0.5,
                method="STATIC",
                evidence="ancient",
                replay_supported=False,
                computed_at=anchor - timedelta(days=40),
            )
        ]
    )

    result = run_once(backend, control, now=anchor)
    counts = result.per_project["p1"]
    assert counts["scores"] >= 1
    assert counts["causal_edges"] >= 1
    surviving_scores = backend.get_scores("p1", "t-x")
    assert len(surviving_scores) == 1
    assert surviving_scores[0].value == 0.8


def test_retention_skips_disabled_projects(
    backend: DuckDBBackend, control: ControlPlane
) -> None:
    """Disabling the policy (enabled=False) suspends pruning for that project."""
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    control.upsert_retention_policy(
        project_id="p1", hot_days=10, enabled=False
    )
    backend.write_events([_obs(observation_id="old", start_offset_days=40)])
    anchor = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    result = run_once(backend, control, now=anchor)
    assert "p1" not in result.per_project
    # Old row is still there.
    assert backend.list_traces("p1", limit=100)


def test_retention_skips_zero_hot_days(
    backend: DuckDBBackend, control: ControlPlane
) -> None:
    """hot_days=0 disables hot-tier pruning (per §6.4 docstring contract)."""
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    control.upsert_retention_policy(project_id="p1", hot_days=0)
    backend.write_events([_obs(observation_id="old", start_offset_days=999)])
    anchor = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    result = run_once(backend, control, now=anchor)
    assert result.per_project == {}
    assert backend.list_traces("p1", limit=100)


def test_retention_purges_body_offload_files(
    backend: DuckDBBackend, control: ControlPlane, tmp_path: Path
) -> None:
    """Files older than bodies_days under ``{root}/{project_id}/`` are deleted."""
    import os

    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    control.upsert_retention_policy(project_id="p1", hot_days=10, bodies_days=5)
    bodies = tmp_path / "bodies"
    project_dir = bodies / "p1"
    project_dir.mkdir(parents=True)
    fresh = project_dir / "fresh.json"
    old = project_dir / "old.json"
    fresh.write_text("{}")
    old.write_text("{}")
    # Backdate `old` by 10 days; leave fresh as-is.
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))

    anchor = datetime.now(timezone.utc)
    result = run_once(backend, control, now=anchor, bodies_root=bodies)
    assert result.bodies_pruned == 1
    assert fresh.exists()
    assert not old.exists()


def test_retention_worker_lifecycle(
    backend: DuckDBBackend, control: ControlPlane
) -> None:
    """Worker thread starts, runs at least once, then stops cleanly."""
    ws = control.create_workspace(slug="acme", name="Acme")
    control.create_project_with_id(
        project_id="p1", workspace_id=ws["id"], slug="p1", name="p1"
    )
    control.upsert_retention_policy(project_id="p1", hot_days=10)
    backend.write_events([_obs(observation_id="old", start_offset_days=40)])

    worker = RetentionWorker(backend, control, interval_s=60)
    worker.start()
    try:
        for _ in range(60):
            if worker.last_result is not None:
                break
            time.sleep(0.05)
        assert worker.last_result is not None
        assert worker.last_result.per_project["p1"]["events"] == 1
    finally:
        worker.stop()
