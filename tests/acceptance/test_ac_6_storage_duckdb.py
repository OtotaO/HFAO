"""AC §6 acceptance tests — DuckDB subset.

Covers the DuckDB-side subset of §6.7:

    - backend write/read round-trip (parity scaffold)
    - readonly_sql rejects writes (§6.7)
    - readonly_sql enforces project scope (§6.7)

Retention, redaction, DuckLake warm-tier, and body offload tests live in
later commits of Week 8 when those modules exist; see §15.2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hfao.schema.causal import CausalEdge
from hfao.schema.events import (
    CostBreakdown,
    Observation,
    ObservationType,
    Status,
    TokenUsage,
    ToolCall,
)
from hfao.schema.scores import Score
from hfao.storage import StorageBackend
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend


def _obs(
    project_id: str = "p1",
    trace_id: str = "t1",
    observation_id: str = "o1",
    *,
    event_version: int = 1,
    status: Status = "ok",
    name: str = "test",
    obs_type: ObservationType = "GENERATION",
    start_offset: int = 0,
    total_tokens: int = 30,
    input: str | None = None,  # noqa: A002 — matches SPEC §4.1 Observation.input
    output: str | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> Observation:
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
        seconds=start_offset
    )
    return Observation(
        project_id=project_id,
        trace_id=trace_id,
        observation_id=observation_id,
        name=name,
        type=obs_type,
        start_time=now,
        end_time=now + timedelta(milliseconds=120),
        duration_ms=120,
        ingested_at=now,
        status=status,
        input=input,
        output=output,
        usage=TokenUsage(total_tokens=total_tokens),
        cost=CostBreakdown(),
        event_version=event_version,
        tool_calls=tool_calls or [],
    )


@pytest.fixture
def backend() -> DuckDBBackend:
    b = DuckDBBackend(":memory:")
    b.init_schema()
    return b


def test_backend_protocol_conformance(backend: DuckDBBackend) -> None:
    assert isinstance(backend, StorageBackend)


def test_duckdb_event_roundtrip(backend: DuckDBBackend) -> None:
    obs = _obs(tool_calls=[ToolCall(id="c1", name="search", arguments='{"q":"x"}')])
    assert backend.write_events([obs]) == 1
    got = backend.get_trace("p1", "t1")
    assert len(got) == 1
    assert got[0].observation_id == "o1"
    assert got[0].usage.total_tokens == 30
    assert got[0].tool_calls[0].name == "search"


def test_duckdb_event_version_dedup(backend: DuckDBBackend) -> None:
    backend.write_events([_obs(event_version=1, status="ok")])
    backend.write_events([_obs(event_version=2, status="error")])
    got = backend.get_trace("p1", "t1")
    assert len(got) == 1
    assert got[0].status == "error"
    assert got[0].event_version == 2


def test_duckdb_list_traces_aggregates(backend: DuckDBBackend) -> None:
    backend.write_events(
        [
            _obs(trace_id="t1", observation_id="o1", start_offset=0, total_tokens=10),
            _obs(
                trace_id="t1",
                observation_id="o2",
                start_offset=1,
                total_tokens=20,
                status="error",
            ),
            _obs(trace_id="t2", observation_id="o3", start_offset=5, total_tokens=5),
        ]
    )
    traces = backend.list_traces("p1")
    by_id = {t["trace_id"]: t for t in traces}
    assert by_id["t1"]["span_count"] == 2
    assert by_id["t1"]["total_tokens"] == 30
    assert by_id["t1"]["has_error"] is True
    assert by_id["t2"]["has_error"] is False


def test_duckdb_list_traces_rejects_dangerous_where(backend: DuckDBBackend) -> None:
    backend.write_events([_obs()])
    with pytest.raises(PermissionError):
        backend.list_traces("p1", where_sql="1=1; DELETE FROM events")
    with pytest.raises(PermissionError):
        backend.list_traces("p1", where_sql="1=1 OR (DROP TABLE events)")


def test_duckdb_search_traces_text(backend: DuckDBBackend) -> None:
    backend.write_events(
        [
            _obs(observation_id="o1"),
            _obs(observation_id="o2", input="capital of france", output="Paris"),
        ]
    )
    hits = backend.search_traces_text("p1", "Paris")
    assert any(h["trace_id"] == "t1" for h in hits)


def test_duckdb_scores_and_causal_edges(backend: DuckDBBackend) -> None:
    now = datetime.now(timezone.utc)
    backend.write_scores(
        [
            Score(
                project_id="p1",
                trace_id="t1",
                name="accuracy",
                value=0.92,
                source="LLM_JUDGE",
                timestamp=now,
            )
        ]
    )
    backend.write_causal_edges(
        [
            CausalEdge(
                project_id="p1",
                trace_id="t1",
                source_observation_id="o1",
                target_observation_id="o2",
                edge_type="HANDOFF",
                confidence=0.8,
                method="STATIC",
                evidence="handoff_target_agent_id set",
                replay_supported=True,
                computed_at=now,
            )
        ]
    )
    scores = backend.get_scores("p1", "t1")
    edges = backend.get_causal_edges("p1", "t1")
    assert scores[0].name == "accuracy"
    assert edges[0].edge_type == "HANDOFF"
    assert edges[0].replay_supported is True


def test_readonly_sql_enforces_project_scope(backend: DuckDBBackend) -> None:
    backend.write_events(
        [
            _obs(project_id="p1", trace_id="t1", observation_id="o1", total_tokens=10),
            _obs(project_id="p2", trace_id="t2", observation_id="o2", total_tokens=99),
        ]
    )
    p1_rows = backend.execute_readonly_sql(
        "p1", "SELECT count() AS n FROM events_current"
    )
    p2_rows = backend.execute_readonly_sql(
        "p2", "SELECT sum(total_tokens) AS s FROM events_current"
    )
    assert p1_rows[0]["n"] == 1
    assert p2_rows[0]["s"] == 99

    # p1 cannot see p2 even by naming it directly
    leak = backend.execute_readonly_sql(
        "p1", "SELECT count() AS n FROM events_current WHERE project_id = 'p2'"
    )
    assert leak[0]["n"] == 0


def test_readonly_sql_rejects_writes(backend: DuckDBBackend) -> None:
    for bad in [
        "DELETE FROM events",
        "INSERT INTO events VALUES (1)",
        "DROP TABLE events",
        "UPDATE events SET status = 'ok'",
        "SELECT 1; DROP TABLE events",
        "ALTER TABLE events ADD COLUMN x INT",
        "ATTACH 'other.db' AS o",
    ]:
        with pytest.raises(PermissionError):
            backend.execute_readonly_sql("p1", bad)


def test_control_plane_workspace_project_api_key(tmp_path: Path) -> None:
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws = cp.create_workspace(slug="acme", name="Acme")
    proj = cp.create_project(workspace_id=ws["id"], slug="demo", name="Demo")
    raw, meta = cp.issue_api_key(workspace_id=ws["id"], role="owner", name="cli")
    assert raw.startswith("hfao_pat_")
    verified = cp.verify_api_key(raw)
    assert verified is not None and verified["role"] == "owner"
    assert cp.verify_api_key("hfao_pat_nope") is None
    cp.revoke_api_key(meta["id"])
    assert cp.verify_api_key(raw) is None
    assert proj["workspace_id"] == ws["id"]


def test_control_plane_prompt_versioning_and_labels(tmp_path: Path) -> None:
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws = cp.create_workspace(slug="acme", name="Acme")
    proj = cp.create_project(workspace_id=ws["id"], slug="demo", name="Demo")
    v1 = cp.create_prompt_version(
        project_id=proj["id"], name="greeter", type="text",
        content="Hi {{name}}", created_by="alice",
    )
    v2 = cp.create_prompt_version(
        project_id=proj["id"], name="greeter", type="text",
        content="Hello {{name}}", created_by="alice",
    )
    assert (v1["version"], v2["version"]) == (1, 2)
    cp.set_prompt_label(
        project_id=proj["id"], name="greeter", label="production", version=2
    )
    prod = cp.get_prompt(project_id=proj["id"], name="greeter", label="production")
    assert prod is not None and prod["version"] == 2
    cp.set_prompt_label(
        project_id=proj["id"], name="greeter", label="production", version=1
    )
    prod = cp.get_prompt(project_id=proj["id"], name="greeter", label="production")
    assert prod is not None and prod["version"] == 1


def test_control_plane_dataset_crud(tmp_path: Path) -> None:
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws = cp.create_workspace(slug="acme", name="Acme")
    proj = cp.create_project(workspace_id=ws["id"], slug="demo", name="Demo")
    ds = cp.create_dataset(project_id=proj["id"], name="goldens", description="d")
    assert ds["id"].startswith("ds_")
    assert cp.get_dataset(project_id=proj["id"], dataset_id=ds["id"])["name"] == "goldens"
    item = cp.add_dataset_item(
        project_id=proj["id"],
        dataset_id=ds["id"],
        input='{"q": "2+2"}',
        expected_output='{"a": "4"}',
        metadata={"split": "train"},
        source_trace_id="t1",
    )
    assert item["source_trace_id"] == "t1"
    items = cp.list_dataset_items(project_id=proj["id"], dataset_id=ds["id"])
    assert len(items) == 1 and items[0]["expected_output"] == '{"a": "4"}'
    assert [d["id"] for d in cp.list_datasets(project_id=proj["id"])] == [ds["id"]]
    # Adding an item to a non-existent dataset is rejected.
    with pytest.raises(KeyError):
        cp.add_dataset_item(project_id=proj["id"], dataset_id="ds_nope", input="x")


def test_control_plane_annotation_queue_crud(tmp_path: Path) -> None:
    cp = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    cp.init_schema()
    ws = cp.create_workspace(slug="acme", name="Acme")
    proj = cp.create_project(workspace_id=ws["id"], slug="demo", name="Demo")
    q = cp.create_annotation_queue(
        project_id=proj["id"],
        name="errors",
        filter_query="status = 'error'",
        score_schema=["quality", "helpfulness"],
    )
    assert q["id"].startswith("aq_")
    assert [x["id"] for x in cp.list_annotation_queues(project_id=proj["id"])] == [q["id"]]
    cp.enqueue_annotation_item(queue_id=q["id"], trace_id="t1")
    cp.enqueue_annotation_item(queue_id=q["id"], trace_id="t2", observation_id="o9")
    pending = cp.list_annotation_items(queue_id=q["id"], status="pending")
    assert len(pending) == 2
    cp.set_annotation_item_status(
        queue_id=q["id"], trace_id="t1", observation_id=None,
        status="completed", completed_at="2026-05-29T00:00:00+00:00",
    )
    assert len(cp.list_annotation_items(queue_id=q["id"], status="pending")) == 1
    done = cp.list_annotation_items(queue_id=q["id"], status="completed")
    assert len(done) == 1 and done[0]["trace_id"] == "t1"
    # Re-enqueue is idempotent on the (queue, trace, observation) key.
    cp.enqueue_annotation_item(queue_id=q["id"], trace_id="t2", observation_id="o9")
    assert len(cp.list_annotation_items(queue_id=q["id"])) == 2
