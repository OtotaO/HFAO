"""DuckDB storage backend.

SPEC §4.2 (DDL), §6.1 (storage matrix), §6.2 (protocol), §6.7 (AC).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import msgspec

from hfao.schema.causal import CausalEdge
from hfao.schema.events import CostBreakdown, Observation, TokenUsage, ToolCall
from hfao.schema.scores import Score

_DDL_PATH = Path(__file__).parent / "ddl" / "duckdb.sql"

_ALLOWED_READONLY_PREFIXES = ("select", "with", "show", "describe", "pragma")
_FORBIDDEN_READONLY = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|truncate|"
    r"pragma\s+(?!table_info|database_list|show_tables))\b",
    re.IGNORECASE,
)


class DuckDBBackend:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._con = duckdb.connect(self._path)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def init_schema(self) -> None:
        with self._lock:
            self._con.execute(_DDL_PATH.read_text())

    def write_events(self, events: Iterable[Observation]) -> int:
        rows = [_event_row(e) for e in events]
        if not rows:
            return 0
        sql = f"INSERT OR REPLACE INTO events VALUES ({', '.join(['?'] * len(rows[0]))})"
        with self._lock:
            self._con.executemany(sql, rows)
        return len(rows)

    def write_scores(self, scores: Iterable[Score]) -> int:
        rows = [_score_row(s) for s in scores]
        if not rows:
            return 0
        sql = f"INSERT OR REPLACE INTO scores VALUES ({', '.join(['?'] * len(rows[0]))})"
        with self._lock:
            self._con.executemany(sql, rows)
        return len(rows)

    def write_causal_edges(self, edges: Iterable[CausalEdge]) -> int:
        rows = [_edge_row(e) for e in edges]
        if not rows:
            return 0
        sql = f"INSERT OR REPLACE INTO causal_edges VALUES ({', '.join(['?'] * len(rows[0]))})"
        with self._lock:
            self._con.executemany(sql, rows)
        return len(rows)

    def get_trace(self, project_id: str, trace_id: str) -> list[Observation]:
        with self._lock:
            cur = self._con.execute(
                "SELECT * FROM events_current "
                "WHERE project_id = ? AND trace_id = ? "
                "ORDER BY start_time, observation_id",
                [project_id, trace_id],
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [_row_to_observation(dict(zip(cols, r, strict=True))) for r in rows]

    def list_traces(
        self,
        project_id: str,
        *,
        where_sql: str = "1=1",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        _assert_where_fragment(where_sql)
        sql = f"""
        SELECT trace_id,
               min(start_time) AS first_start,
               max(coalesce(end_time, start_time)) AS last_end,
               count() AS span_count,
               sum(total_tokens) AS total_tokens,
               sum(total_cost_usd) AS total_cost_usd,
               bool_or(status = 'error') AS has_error,
               any_value(session_id) AS session_id,
               any_value(user_id) AS user_id
        FROM events_current
        WHERE project_id = ? AND ({where_sql})
        GROUP BY trace_id
        ORDER BY first_start DESC
        LIMIT ? OFFSET ?
        """
        with self._lock:
            cur = self._con.execute(sql, [project_id, limit, offset])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def search_traces_text(
        self, project_id: str, query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        like = f"%{query}%"
        sql = """
        SELECT DISTINCT trace_id, name, start_time
        FROM events_current
        WHERE project_id = ?
          AND (input ILIKE ? OR output ILIKE ? OR name ILIKE ?)
        ORDER BY start_time DESC
        LIMIT ?
        """
        with self._lock:
            cur = self._con.execute(sql, [project_id, like, like, like, limit])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def get_causal_edges(self, project_id: str, trace_id: str) -> list[CausalEdge]:
        with self._lock:
            cur = self._con.execute(
                "SELECT * FROM causal_edges WHERE project_id = ? AND trace_id = ?",
                [project_id, trace_id],
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [
            CausalEdge(
                project_id=r["project_id"],
                trace_id=r["trace_id"],
                source_observation_id=r["source_observation_id"],
                target_observation_id=r["target_observation_id"],
                edge_type=r["edge_type"],
                confidence=r["confidence"],
                method=r["method"],
                evidence=r["evidence"] or "",
                replay_supported=bool(r["replay_supported"]),
                judge_model=r["judge_model"],
                computed_at=r["computed_at"],
            )
            for r in (dict(zip(cols, row, strict=True)) for row in rows)
        ]

    def get_scores(self, project_id: str, trace_id: str) -> list[Score]:
        with self._lock:
            cur = self._con.execute(
                "SELECT * FROM scores WHERE project_id = ? AND trace_id = ?",
                [project_id, trace_id],
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        out: list[Score] = []
        for row in rows:
            r = dict(zip(cols, row, strict=True))
            out.append(
                Score(
                    project_id=r["project_id"],
                    trace_id=r["trace_id"],
                    observation_id=r["observation_id"] or None,
                    name=r["name"],
                    value=r["value"],
                    string_value=r["string_value"],
                    source=r["source"],
                    comment=r["comment"],
                    judge_model=r["judge_model"],
                    calibration_bias=r["calibration_bias"] or 0.0,
                    timestamp=r["timestamp"],
                    annotator_id=r["annotator_id"],
                    eval_run_id=r["eval_run_id"],
                )
            )
        return out

    def export_events_to_parquet(
        self,
        project_id: str,
        *,
        start: datetime,
        end: datetime,
        out_path: str,
    ) -> int:
        """Materialise events in ``[start, end)`` for ``project_id`` as Parquet.

        Returns the row count written. Used by ``hfao parquet export`` per
        §16 Q-13. Keeps the COPY ... TO 'foo.parquet' inside the storage
        boundary (Appendix C rule 4).
        """
        with self._lock:
            pre = self._con.execute(
                "SELECT count() FROM events_current "
                "WHERE project_id = ? AND start_time >= ? AND start_time < ?",
                [project_id, start, end],
            ).fetchone()
            count = int(pre[0]) if pre else 0
            if count == 0:
                return 0
            safe_path = out_path.replace("'", "''")
            self._con.execute(
                f"COPY (SELECT * FROM events_current "
                f"WHERE project_id = ? AND start_time >= ? AND start_time < ?) "
                f"TO '{safe_path}' (FORMAT PARQUET)",
                [project_id, start, end],
            )
        return count

    def purge_old(
        self, project_id: str, *, before: datetime
    ) -> dict[str, int]:
        """DELETE rows older than ``before`` (§6.4). Returns counts per table."""
        purge_specs = (
            ("events", "start_time"),
            ("scores", "timestamp"),
            ("causal_edges", "computed_at"),
        )
        counts: dict[str, int] = {}
        with self._lock:
            for table, column in purge_specs:
                pre = self._con.execute(
                    f"SELECT count() FROM {table} "
                    f"WHERE project_id = ? AND {column} < ?",
                    [project_id, before],
                ).fetchone()
                pre_count = int(pre[0]) if pre else 0
                self._con.execute(
                    f"DELETE FROM {table} "
                    f"WHERE project_id = ? AND {column} < ?",
                    [project_id, before],
                )
                counts[table] = pre_count
        return counts

    def refresh_cost_rollup(self) -> int:
        """Rebuild cost_daily from events_current (§8.3).

        Aggregates by (project_id, date, user_id, agent_id, model, prompt_name)
        using empty-string sentinels for NULL group keys (§4.5 rule 4).
        Idempotent: deletes and re-inserts the whole table; safe to call from
        a periodic worker (60s cadence per §8.3).
        """
        sql = """
        DELETE FROM cost_daily;
        INSERT INTO cost_daily
        SELECT project_id,
               CAST(start_time AS DATE) AS date,
               COALESCE(user_id, '')     AS user_id,
               COALESCE(agent_id, '')    AS agent_id,
               COALESCE(model, '')       AS model,
               COALESCE(prompt_name, '') AS prompt_name,
               SUM(total_cost_usd)       AS total_cost_usd,
               SUM(total_tokens)         AS total_tokens,
               COUNT(*)                  AS call_count
        FROM events_current
        GROUP BY project_id, date, user_id, agent_id, model, prompt_name;
        """
        with self._lock:
            self._con.execute(sql)
            cur = self._con.execute("SELECT count() FROM cost_daily")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def cost_rollup(
        self,
        project_id: str,
        *,
        date_from: datetime,
        date_to: datetime,
        group_by: list[str],
    ) -> list[dict[str, Any]]:
        allowed = {"user_id", "agent_id", "model", "prompt_name", "date"}
        bad = set(group_by) - allowed
        if bad:
            raise ValueError(f"Invalid group_by columns: {sorted(bad)}")
        select_cols = ", ".join(group_by) if group_by else "date"
        group_clause = select_cols
        sql = f"""
        SELECT {select_cols},
               sum(total_cost_usd) AS total_cost_usd,
               sum(total_tokens)   AS total_tokens,
               sum(call_count)     AS call_count
        FROM cost_daily
        WHERE project_id = ? AND date >= ? AND date <= ?
        GROUP BY {group_clause}
        ORDER BY 1
        """
        with self._lock:
            cur = self._con.execute(sql, [project_id, date_from.date(), date_to.date()])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def execute_readonly_sql(self, project_id: str, sql: str) -> list[dict[str, Any]]:
        _assert_read_only(sql)
        scoped = _scope_project(sql, project_id)
        with self._lock:
            cur = self._con.execute(scoped)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _assert_where_fragment(where_sql: str) -> None:
    if ";" in where_sql:
        raise PermissionError("Semicolons are not allowed in where_sql")
    if _FORBIDDEN_READONLY.search(where_sql):
        raise PermissionError("Write/DDL keywords are not allowed in where_sql")


def _assert_read_only(sql: str) -> None:
    stripped = sql.strip().lower()
    if not any(stripped.startswith(p) for p in _ALLOWED_READONLY_PREFIXES):
        raise PermissionError("Only SELECT/WITH/SHOW/DESCRIBE/PRAGMA queries are allowed")
    if _FORBIDDEN_READONLY.search(sql):
        raise PermissionError("Write/DDL statements are not allowed here")
    if ";" in sql.rstrip(";").rstrip():
        raise PermissionError("Multiple statements are not allowed")


_SCOPED_TABLES = ("events", "events_current", "scores", "causal_edges", "cost_daily")


def _scope_project(sql: str, project_id: str) -> str:
    """Wrap user SQL in CTEs that shadow base tables with project-filtered views.

    In DuckDB, a CTE named `events` hides the base `events` table within the
    query, so arbitrary user SELECTs against the canonical names stay scoped.
    """
    safe = project_id.replace("'", "''")
    ctes = ",\n".join(
        f"{t} AS (SELECT * FROM main.{t} WHERE project_id = '{safe}')"
        for t in _SCOPED_TABLES
    )
    return f"WITH {ctes}\n{sql}"


def _event_row(e: Observation) -> tuple[Any, ...]:
    usage: TokenUsage = e.usage
    cost: CostBreakdown = e.cost
    return (
        e.project_id,
        e.trace_id,
        e.observation_id,
        e.parent_observation_id,
        e.session_id,
        e.user_id,
        e.environment,
        e.release,
        e.name,
        e.type,
        e.level,
        e.start_time,
        e.end_time,
        e.duration_ms,
        e.status,
        e.status_message,
        e.input,
        e.output,
        e.input_ref,
        e.output_ref,
        e.model,
        dict(e.model_parameters),
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        usage.total_tokens,
        cost.input_cost_usd,
        cost.output_cost_usd,
        cost.total_cost_usd,
        dict(e.tool_definitions),
        [msgspec.json.encode(tc).decode() for tc in e.tool_calls],
        list(e.tool_call_names),
        e.agent_id,
        e.agent_role,
        e.handoff_target_agent_id,
        e.prompt_name,
        e.prompt_version,
        e.prompt_label,
        dict(e.metadata),
        list(e.tags),
        e.event_version,
        e.ingested_at,
    )


def _score_row(s: Score) -> tuple[Any, ...]:
    return (
        s.project_id,
        s.trace_id,
        s.observation_id or "",
        s.name,
        s.value,
        s.string_value,
        s.source,
        s.comment,
        s.judge_model,
        s.calibration_bias,
        s.timestamp,
        s.annotator_id,
        s.eval_run_id,
    )


def _edge_row(e: CausalEdge) -> tuple[Any, ...]:
    return (
        e.project_id,
        e.trace_id,
        e.source_observation_id,
        e.target_observation_id,
        e.edge_type,
        e.confidence,
        e.method,
        e.evidence,
        e.replay_supported,
        e.judge_model,
        e.computed_at,
    )


def _row_to_observation(r: dict[str, Any]) -> Observation:
    tool_calls_raw: list[str] = list(r.get("tool_calls") or [])
    tool_calls: list[ToolCall] = []
    for raw in tool_calls_raw:
        try:
            tool_calls.append(msgspec.json.decode(raw.encode(), type=ToolCall))
        except msgspec.DecodeError:
            d: dict[str, Any] = json.loads(raw)
            tool_calls.append(
                ToolCall(
                    id=d.get("id", ""),
                    name=d.get("name", ""),
                    arguments=d.get("arguments", ""),
                    result=d.get("result"),
                    error=d.get("error"),
                )
            )
    return Observation(
        project_id=r["project_id"],
        trace_id=r["trace_id"],
        observation_id=r["observation_id"],
        parent_observation_id=r.get("parent_observation_id"),
        session_id=r.get("session_id"),
        user_id=r.get("user_id"),
        environment=r.get("environment") or "production",
        release=r.get("release"),
        name=r["name"],
        type=r["type"],
        level=r.get("level") or "DEFAULT",
        start_time=r["start_time"],
        end_time=r.get("end_time"),
        duration_ms=r.get("duration_ms"),
        status=r.get("status") or "unset",
        status_message=r.get("status_message"),
        input=r.get("input"),
        output=r.get("output"),
        input_ref=r.get("input_ref"),
        output_ref=r.get("output_ref"),
        model=r.get("model"),
        model_parameters=dict(r.get("model_parameters") or {}),
        usage=TokenUsage(
            prompt_tokens=r.get("prompt_tokens") or 0,
            completion_tokens=r.get("completion_tokens") or 0,
            cache_read_tokens=r.get("cache_read_tokens") or 0,
            cache_creation_tokens=r.get("cache_creation_tokens") or 0,
            total_tokens=r.get("total_tokens") or 0,
        ),
        cost=CostBreakdown(
            input_cost_usd=r.get("input_cost_usd") or 0.0,
            output_cost_usd=r.get("output_cost_usd") or 0.0,
            total_cost_usd=r.get("total_cost_usd") or 0.0,
        ),
        tool_definitions=dict(r.get("tool_definitions") or {}),
        tool_calls=tool_calls,
        tool_call_names=list(r.get("tool_call_names") or []),
        agent_id=r.get("agent_id"),
        agent_role=r.get("agent_role"),
        handoff_target_agent_id=r.get("handoff_target_agent_id"),
        prompt_name=r.get("prompt_name"),
        prompt_version=r.get("prompt_version"),
        prompt_label=r.get("prompt_label"),
        metadata=dict(r.get("metadata") or {}),
        tags=list(r.get("tags") or []),
        event_version=r["event_version"],
        ingested_at=r["ingested_at"],
    )


__all__ = ["DuckDBBackend"]
