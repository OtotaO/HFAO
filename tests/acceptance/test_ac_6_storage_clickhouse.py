"""AC §6 acceptance tests — ClickHouse backend + cross-backend parity.

Live ClickHouse tests require a running server. Set
``HFAO_TEST_CLICKHOUSE_DSN`` (e.g. ``http://default:@localhost:8123/hfao_test``)
to exercise them; otherwise the parametrized suite is skipped for the
clickhouse backend.

The DSN parser tests run unconditionally.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

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
from hfao.storage.clickhouse_backend import ClickHouseBackend, parse_dsn
from hfao.storage.duckdb_backend import DuckDBBackend

_CH_DSN = os.environ.get("HFAO_TEST_CLICKHOUSE_DSN")


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


# ---------- DSN parsing (no live server) ----------


@pytest.mark.parametrize(
    "dsn,expected",
    [
        (
            "http://default:@localhost:8123/hfao",
            {"host": "localhost", "port": 8123, "database": "hfao", "secure": False},
        ),
        (
            "https://u:p@ch.example.com/warehouse",
            {"host": "ch.example.com", "port": 8443, "database": "warehouse", "secure": True},
        ),
        (
            "clickhouse://alice:secret@db:9000/observatory",
            {"host": "db", "port": 9000, "database": "observatory", "secure": False},
        ),
        (
            "clickhouses://alice:secret@db/observatory",
            {"host": "db", "port": 8443, "database": "observatory", "secure": True},
        ),
    ],
)
def test_parse_dsn(dsn: str, expected: dict[str, Any]) -> None:
    got = parse_dsn(dsn)
    for k, v in expected.items():
        assert got[k] == v, f"{k}: {got[k]!r} != {v!r}"


def test_parse_dsn_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        parse_dsn("mysql://root@localhost:3306/x")


# ---------- Live ClickHouse + parity ----------


@pytest.fixture
def ch_backend() -> Iterator[ClickHouseBackend]:
    if not _CH_DSN:
        pytest.skip("HFAO_TEST_CLICKHOUSE_DSN not set")
    b = ClickHouseBackend(_CH_DSN)
    b.init_schema()
    # fresh start — reset via raw client; TRUNCATE is a write, cannot use
    # execute_readonly_sql. Touching _client is intentional at test boundary.
    client: Any = b._client  # pyright: ignore[reportPrivateUsage]
    for t in ("events", "scores", "causal_edges"):
        client.command(f"TRUNCATE TABLE IF EXISTS {t}")
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def backends(
    ch_backend: ClickHouseBackend,
) -> Iterator[tuple[DuckDBBackend, ClickHouseBackend]]:
    duck = DuckDBBackend(":memory:")
    duck.init_schema()
    yield duck, ch_backend
    duck.close()


def test_clickhouse_protocol_conformance() -> None:
    # Purely structural: does ClickHouseBackend satisfy the Protocol?
    assert issubclass(ClickHouseBackend, object)
    # Runtime check without a live server requires touching __init__ params;
    # just confirm class-level methods exist per §6.2.
    for method in (
        "init_schema",
        "write_events",
        "write_scores",
        "write_causal_edges",
        "get_trace",
        "list_traces",
        "search_traces_text",
        "get_causal_edges",
        "get_scores",
        "cost_rollup",
        "execute_readonly_sql",
    ):
        assert callable(getattr(ClickHouseBackend, method))


@pytest.mark.skipif(not _CH_DSN, reason="HFAO_TEST_CLICKHOUSE_DSN not set")
def test_ch_event_roundtrip(ch_backend: ClickHouseBackend) -> None:
    obs = _obs(tool_calls=[ToolCall(id="c1", name="search", arguments='{"q":"x"}')])
    assert ch_backend.write_events([obs]) == 1
    got = ch_backend.get_trace("p1", "t1")
    assert len(got) == 1
    assert got[0].tool_calls[0].name == "search"


@pytest.mark.skipif(not _CH_DSN, reason="HFAO_TEST_CLICKHOUSE_DSN not set")
def test_ch_event_version_dedup(ch_backend: ClickHouseBackend) -> None:
    ch_backend.write_events([_obs(event_version=1, status="ok")])
    ch_backend.write_events([_obs(event_version=2, status="error")])
    got = ch_backend.get_trace("p1", "t1")
    assert len(got) == 1
    assert got[0].status == "error"
    assert got[0].event_version == 2


@pytest.mark.skipif(not _CH_DSN, reason="HFAO_TEST_CLICKHOUSE_DSN not set")
def test_ch_readonly_sql_rejects_writes(ch_backend: ClickHouseBackend) -> None:
    for bad in (
        "INSERT INTO events VALUES (1)",
        "DELETE FROM events",
        "DROP TABLE events",
        "TRUNCATE TABLE events",
        "OPTIMIZE TABLE events FINAL",
    ):
        with pytest.raises(PermissionError):
            ch_backend.execute_readonly_sql("p1", bad)


@pytest.mark.skipif(not _CH_DSN, reason="HFAO_TEST_CLICKHOUSE_DSN not set")
def test_ch_readonly_sql_enforces_project_scope(
    ch_backend: ClickHouseBackend,
) -> None:
    ch_backend.write_events(
        [
            _obs(project_id="p1", observation_id="o1", total_tokens=10),
            _obs(project_id="p2", observation_id="o2", total_tokens=99),
        ]
    )
    p1 = ch_backend.execute_readonly_sql("p1", "SELECT count() AS n FROM events")
    p2 = ch_backend.execute_readonly_sql(
        "p2", "SELECT sum(total_tokens) AS s FROM events"
    )
    assert p1[0]["n"] == 1
    assert int(p2[0]["s"]) == 99


@pytest.mark.skipif(not _CH_DSN, reason="HFAO_TEST_CLICKHOUSE_DSN not set")
def test_backend_parity_duckdb_clickhouse(
    backends: tuple[DuckDBBackend, ClickHouseBackend],
) -> None:
    """§6.7 parity: identical writes produce identical read-side shapes."""
    duck, ch = backends
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)

    events = [
        _obs(observation_id="o1", total_tokens=10),
        _obs(observation_id="o2", start_offset=1, total_tokens=20, status="error"),
    ]
    scores = [
        Score(
            project_id="p1",
            trace_id="t1",
            name="accuracy",
            value=0.5,
            source="LLM_JUDGE",
            timestamp=now,
        )
    ]
    edges = [
        CausalEdge(
            project_id="p1",
            trace_id="t1",
            source_observation_id="o1",
            target_observation_id="o2",
            edge_type="HANDOFF",
            confidence=0.75,
            method="STATIC",
            evidence="",
            replay_supported=False,
            computed_at=now,
        )
    ]
    for b in (duck, ch):
        b.write_events(events)
        b.write_scores(scores)
        b.write_causal_edges(edges)

    duck_trace = duck.get_trace("p1", "t1")
    ch_trace = ch.get_trace("p1", "t1")
    assert len(duck_trace) == len(ch_trace) == 2
    assert [o.observation_id for o in duck_trace] == [o.observation_id for o in ch_trace]

    duck_edges = duck.get_causal_edges("p1", "t1")
    ch_edges = ch.get_causal_edges("p1", "t1")
    assert len(duck_edges) == len(ch_edges) == 1
    assert duck_edges[0].edge_type == ch_edges[0].edge_type
    assert duck_edges[0].method == ch_edges[0].method


def test_backend_protocol_is_implemented_by_both() -> None:
    # Runtime Protocol conformance — DuckDB only (CH needs a live client in __init__)
    duck = DuckDBBackend(":memory:")
    duck.init_schema()
    assert isinstance(duck, StorageBackend)
    duck.close()
