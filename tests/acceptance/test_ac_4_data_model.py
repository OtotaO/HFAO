from datetime import datetime, timezone

import msgspec
from hfao.schema.events import Observation


def test_msgspec_struct_roundtrip():
    obs = Observation(
        project_id="p1",
        trace_id="t1",
        observation_id="o1",
        name="test",
        type="GENERATION",
        start_time=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc)
    )
    data = msgspec.json.encode(obs)
    decoded = msgspec.json.decode(data, type=Observation)
    assert decoded.project_id == obs.project_id
    assert decoded.trace_id == obs.trace_id

def test_duckdb_ddl_applies_clean(tmp_path):
    import duckdb
    db_path = str(tmp_path / "test.duckdb")
    con = duckdb.connect(db_path)
    with open("packages/hfao/storage/ddl/duckdb.sql") as f:
        ddl = f.read()
    con.execute(ddl)
    # Check if tables exist
    tables = con.execute("SHOW TABLES").fetchall()
    table_names = [t[0] for t in tables]
    assert "events" in table_names
    assert "scores" in table_names
    assert "causal_edges" in table_names
    assert "cost_daily" in table_names
    con.close()

def test_clickhouse_ddl_syntax():
    # We can't easily run ClickHouse here without a container, but we can check the file exists
    import os
    assert os.path.exists("packages/hfao/storage/ddl/clickhouse.sql")
    with open("packages/hfao/storage/ddl/clickhouse.sql") as f:
        content = f.read()
    assert "CREATE TABLE IF NOT EXISTS events" in content

def test_no_null_in_pk():
    # This is more of a DDL check.
    # In duckdb.sql: PRIMARY KEY (project_id, trace_id, observation_id, event_version)
    # The columns are defined as NOT NULL.
    with open("packages/hfao/storage/ddl/duckdb.sql") as f:
        content = f.read()
    assert "project_id              VARCHAR NOT NULL" in content
    assert "trace_id                VARCHAR NOT NULL" in content
    assert "observation_id          VARCHAR NOT NULL" in content
    assert "event_version           BIGINT NOT NULL" in content
