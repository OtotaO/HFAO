"""ClickHouse storage backend.

SPEC §4.3 (DDL), §6.1 (Docker/K8s shape), §6.2 (StorageBackend protocol),
§6.7 (AC). Uses clickhouse-connect over HTTP(S); DSN is parsed from
HFAO_CLICKHOUSE_DSN.

ReplacingMergeTree dedups on merge, so reads use FINAL for deterministic
behavior. This is the accepted cost for the canonical ``events_current``
semantics documented in §4.2.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import msgspec

from hfao.schema.causal import CausalEdge
from hfao.schema.events import CostBreakdown, Observation, TokenUsage, ToolCall
from hfao.schema.scores import Score

_DDL_PATH = Path(__file__).parent / "ddl" / "clickhouse.sql"

_ALLOWED_READONLY_PREFIXES = ("select", "with", "show", "describe")
_FORBIDDEN_READONLY = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|truncate|optimize|rename)\b",
    re.IGNORECASE,
)

_EVENT_COLUMNS = [
    "project_id",
    "trace_id",
    "observation_id",
    "parent_observation_id",
    "session_id",
    "user_id",
    "environment",
    "release",
    "name",
    "type",
    "level",
    "start_time",
    "end_time",
    "duration_ms",
    "status",
    "status_message",
    "input",
    "output",
    "input_ref",
    "output_ref",
    "model",
    "model_parameters",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_tokens",
    "input_cost_usd",
    "output_cost_usd",
    "total_cost_usd",
    "tool_definitions",
    "tool_calls",
    "tool_call_names",
    "agent_id",
    "agent_role",
    "handoff_target_agent_id",
    "prompt_name",
    "prompt_version",
    "prompt_label",
    "metadata",
    "tags",
    "event_version",
    "ingested_at",
]

_SCORE_COLUMNS = [
    "project_id",
    "trace_id",
    "observation_id",
    "name",
    "value",
    "string_value",
    "source",
    "comment",
    "judge_model",
    "calibration_bias",
    "timestamp",
    "annotator_id",
    "eval_run_id",
    "event_version",
]

_EDGE_COLUMNS = [
    "project_id",
    "trace_id",
    "source_observation_id",
    "target_observation_id",
    "edge_type",
    "confidence",
    "method",
    "evidence",
    "replay_supported",
    "judge_model",
    "computed_at",
    "event_version",
]

_SCOPED_TABLES = ("events", "scores", "causal_edges", "cost_daily_mv")


class ClickHouseBackend:
    # clickhouse-connect ships partial type stubs; widening to Any at the
    # library boundary keeps the rest of the backend strictly typed.
    _client: Any

    def __init__(self, dsn: str) -> None:
        import clickhouse_connect  # noqa: PLC0415

        self._dsn = dsn
        self._lock = threading.RLock()
        params = parse_dsn(dsn)
        self._database: str = str(params["database"])
        # clickhouse-connect stubs leave **kwargs as Unknown; widen here.
        self._client = clickhouse_connect.get_client(  # pyright: ignore[reportUnknownMemberType]
            host=params["host"],
            port=params["port"],
            username=params["username"],
            password=params["password"],
            database=params["database"],
            secure=params["secure"],
        )

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def init_schema(self) -> None:
        with self._lock:
            self._client.command(f"CREATE DATABASE IF NOT EXISTS {self._database}")
            for stmt in _split_ddl(_DDL_PATH.read_text()):
                self._client.command(stmt)

    def write_events(self, events: Iterable[Observation]) -> int:
        rows = [_event_row(e) for e in events]
        if not rows:
            return 0
        with self._lock:
            self._client.insert("events", rows, column_names=_EVENT_COLUMNS)
        return len(rows)

    def write_scores(self, scores: Iterable[Score]) -> int:
        rows = [_score_row(s) for s in scores]
        if not rows:
            return 0
        with self._lock:
            self._client.insert("scores", rows, column_names=_SCORE_COLUMNS)
        return len(rows)

    def write_causal_edges(self, edges: Iterable[CausalEdge]) -> int:
        rows = [_edge_row(e) for e in edges]
        if not rows:
            return 0
        with self._lock:
            self._client.insert("causal_edges", rows, column_names=_EDGE_COLUMNS)
        return len(rows)

    def get_trace(self, project_id: str, trace_id: str) -> list[Observation]:
        sql = (
            "SELECT * FROM events FINAL "
            "WHERE project_id = %(p)s AND trace_id = %(t)s "
            "ORDER BY start_time, observation_id"
        )
        with self._lock:
            res = self._client.query(sql, parameters={"p": project_id, "t": trace_id})
        cols = list(res.column_names)
        return [_row_to_observation(dict(zip(cols, r, strict=True))) for r in res.result_rows]

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
               max(status = 'error') AS has_error,
               any(session_id) AS session_id,
               any(user_id) AS user_id
        FROM events FINAL
        WHERE project_id = %(p)s AND ({where_sql})
        GROUP BY trace_id
        ORDER BY first_start DESC
        LIMIT {int(limit)} OFFSET {int(offset)}
        """
        with self._lock:
            res = self._client.query(sql, parameters={"p": project_id})
        cols = list(res.column_names)
        out: list[dict[str, Any]] = []
        for r in res.result_rows:
            d = dict(zip(cols, r, strict=True))
            d["has_error"] = bool(d.get("has_error", 0))
            out.append(d)
        return out

    def search_traces_text(
        self, project_id: str, query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = """
        SELECT DISTINCT trace_id, name, start_time
        FROM events FINAL
        WHERE project_id = %(p)s
          AND (positionCaseInsensitive(input, %(q)s) > 0
               OR positionCaseInsensitive(output, %(q)s) > 0
               OR positionCaseInsensitive(name, %(q)s) > 0)
        ORDER BY start_time DESC
        LIMIT %(limit)s
        """
        with self._lock:
            res = self._client.query(
                sql, parameters={"p": project_id, "q": query, "limit": limit}
            )
        cols = list(res.column_names)
        return [dict(zip(cols, r, strict=True)) for r in res.result_rows]

    def get_causal_edges(self, project_id: str, trace_id: str) -> list[CausalEdge]:
        sql = (
            "SELECT * FROM causal_edges FINAL "
            "WHERE project_id = %(p)s AND trace_id = %(t)s"
        )
        with self._lock:
            res = self._client.query(sql, parameters={"p": project_id, "t": trace_id})
        cols = list(res.column_names)
        out: list[CausalEdge] = []
        for row in res.result_rows:
            r = dict(zip(cols, row, strict=True))
            out.append(
                CausalEdge(
                    project_id=r["project_id"],
                    trace_id=r["trace_id"],
                    source_observation_id=r["source_observation_id"],
                    target_observation_id=r["target_observation_id"],
                    edge_type=r["edge_type"],
                    confidence=float(r["confidence"]),
                    method=r["method"],
                    evidence=r["evidence"] or "",
                    replay_supported=bool(r["replay_supported"]),
                    judge_model=r["judge_model"] or None,
                    computed_at=r["computed_at"],
                )
            )
        return out

    def get_scores(self, project_id: str, trace_id: str) -> list[Score]:
        sql = (
            "SELECT * FROM scores FINAL "
            "WHERE project_id = %(p)s AND trace_id = %(t)s"
        )
        with self._lock:
            res = self._client.query(sql, parameters={"p": project_id, "t": trace_id})
        cols = list(res.column_names)
        out: list[Score] = []
        for row in res.result_rows:
            r = dict(zip(cols, row, strict=True))
            out.append(
                Score(
                    project_id=r["project_id"],
                    trace_id=r["trace_id"],
                    observation_id=r["observation_id"] or None,
                    name=r["name"],
                    value=r["value"],
                    string_value=r["string_value"] or None,
                    source=r["source"],
                    comment=r["comment"] or None,
                    judge_model=r["judge_model"] or None,
                    calibration_bias=float(r.get("calibration_bias") or 0.0),
                    timestamp=r["timestamp"],
                    annotator_id=r["annotator_id"] or None,
                    eval_run_id=r["eval_run_id"] or None,
                )
            )
        return out

    def purge_old(
        self, project_id: str, *, before: datetime
    ) -> dict[str, int]:
        """ALTER TABLE DELETE — ClickHouse's lightweight mutation path."""
        import contextlib

        before_iso = before.strftime("%Y-%m-%d %H:%M:%S")
        for table, column in (
            ("events", "start_time"),
            ("scores", "timestamp"),
            ("causal_edges", "computed_at"),
        ):
            with contextlib.suppress(Exception):
                self._client.command(
                    f"ALTER TABLE {table} DELETE WHERE project_id = "
                    f"'{project_id}' AND {column} < toDateTime64('{before_iso}', 3)"
                )
        # ClickHouse mutations are async; the row-count response is not
        # meaningful synchronously. Return zeros to keep the protocol
        # contract honest: callers should not rely on the count here.
        return {"events": 0, "scores": 0, "causal_edges": 0}

    def refresh_cost_rollup(self) -> int:
        """No-op: ``cost_daily_mv`` is a ``SummingMergeTree`` materialized view
        that ClickHouse keeps current as events are inserted (§8.3). The
        method exists to satisfy the :class:`StorageBackend` protocol and to
        let cross-backend workers call it without branching."""
        try:
            row = self._client.query("SELECT count() FROM cost_daily_mv").result_rows
            return int(row[0][0]) if row else 0
        except Exception:  # noqa: BLE001 — non-fatal informational return
            return 0

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
        sql = f"""
        SELECT {select_cols},
               sum(total_cost_usd) AS total_cost_usd,
               sum(total_tokens)   AS total_tokens,
               sum(call_count)     AS call_count
        FROM cost_daily_mv
        WHERE project_id = %(p)s AND date >= %(df)s AND date <= %(dt)s
        GROUP BY {select_cols}
        ORDER BY 1
        """
        with self._lock:
            res = self._client.query(
                sql,
                parameters={
                    "p": project_id,
                    "df": date_from.date(),
                    "dt": date_to.date(),
                },
            )
        cols = list(res.column_names)
        return [dict(zip(cols, r, strict=True)) for r in res.result_rows]

    def execute_readonly_sql(self, project_id: str, sql: str) -> list[dict[str, Any]]:
        _assert_read_only(sql)
        scoped = _scope_project(sql, project_id)
        with self._lock:
            res = self._client.query(scoped)
        cols = list(res.column_names)
        return [dict(zip(cols, r, strict=True)) for r in res.result_rows]


def parse_dsn(dsn: str) -> dict[str, Any]:
    parsed = urlparse(dsn)
    scheme = parsed.scheme.lower()
    if scheme not in {"clickhouse", "clickhouses", "http", "https"}:
        raise ValueError(f"Unsupported ClickHouse DSN scheme: {scheme}")
    secure = scheme in {"clickhouses", "https"}
    default_port = 8443 if secure else 8123
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or default_port,
        "username": parsed.username or "default",
        "password": parsed.password or "",
        "database": (parsed.path or "/default").lstrip("/") or "default",
        "secure": secure,
    }


def _split_ddl(text: str) -> list[str]:
    parts = [p.strip() for p in text.split(";")]
    return [p for p in parts if p]


def _assert_where_fragment(where_sql: str) -> None:
    if ";" in where_sql:
        raise PermissionError("Semicolons are not allowed in where_sql")
    if _FORBIDDEN_READONLY.search(where_sql):
        raise PermissionError("Write/DDL keywords are not allowed in where_sql")


def _assert_read_only(sql: str) -> None:
    stripped = sql.strip().lower()
    if not any(stripped.startswith(p) for p in _ALLOWED_READONLY_PREFIXES):
        raise PermissionError("Only SELECT/WITH/SHOW/DESCRIBE queries are allowed")
    if _FORBIDDEN_READONLY.search(sql):
        raise PermissionError("Write/DDL statements are not allowed here")
    if ";" in sql.rstrip(";").rstrip():
        raise PermissionError("Multiple statements are not allowed")


def _scope_project(sql: str, project_id: str) -> str:
    safe = project_id.replace("'", "''")
    ctes = ",\n".join(
        f"{t} AS (SELECT * FROM {t} FINAL WHERE project_id = '{safe}')"
        for t in _SCOPED_TABLES
    )
    return f"WITH {ctes}\n{sql}"


def _event_row(e: Observation) -> list[Any]:
    usage: TokenUsage = e.usage
    cost: CostBreakdown = e.cost
    return [
        e.project_id,
        e.trace_id,
        e.observation_id,
        e.parent_observation_id or "",
        e.session_id or "",
        e.user_id or "",
        e.environment,
        e.release or "",
        e.name,
        e.type,
        e.level,
        e.start_time,
        e.end_time,
        e.duration_ms or 0,
        e.status,
        e.status_message or "",
        e.input or "",
        e.output or "",
        e.input_ref or "",
        e.output_ref or "",
        e.model or "",
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
        e.agent_id or "",
        e.agent_role or "",
        e.handoff_target_agent_id or "",
        e.prompt_name or "",
        e.prompt_version or 0,
        e.prompt_label or "",
        dict(e.metadata),
        list(e.tags),
        e.event_version,
        e.ingested_at,
    ]


def _score_row(s: Score) -> list[Any]:
    return [
        s.project_id,
        s.trace_id,
        s.observation_id or "",
        s.name,
        s.value,
        s.string_value or "",
        s.source,
        s.comment or "",
        s.judge_model or "",
        s.calibration_bias,
        s.timestamp,
        s.annotator_id or "",
        s.eval_run_id or "",
        1,  # event_version
    ]


def _edge_row(e: CausalEdge) -> list[Any]:
    return [
        e.project_id,
        e.trace_id,
        e.source_observation_id,
        e.target_observation_id,
        e.edge_type,
        e.confidence,
        e.method,
        e.evidence,
        e.replay_supported,
        e.judge_model or "",
        e.computed_at,
        1,  # event_version
    ]


def _row_to_observation(r: dict[str, Any]) -> Observation:
    import json

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
        parent_observation_id=r.get("parent_observation_id") or None,
        session_id=r.get("session_id") or None,
        user_id=r.get("user_id") or None,
        environment=r.get("environment") or "production",
        release=r.get("release") or None,
        name=r["name"],
        type=r["type"],
        level=r.get("level") or "DEFAULT",
        start_time=r["start_time"],
        end_time=r.get("end_time"),
        duration_ms=r.get("duration_ms"),
        status=r.get("status") or "unset",
        status_message=r.get("status_message") or None,
        input=r.get("input") or None,
        output=r.get("output") or None,
        input_ref=r.get("input_ref") or None,
        output_ref=r.get("output_ref") or None,
        model=r.get("model") or None,
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
        agent_id=r.get("agent_id") or None,
        agent_role=r.get("agent_role") or None,
        handoff_target_agent_id=r.get("handoff_target_agent_id") or None,
        prompt_name=r.get("prompt_name") or None,
        prompt_version=r.get("prompt_version") or None,
        prompt_label=r.get("prompt_label") or None,
        metadata=dict(r.get("metadata") or {}),
        tags=list(r.get("tags") or []),
        event_version=int(r["event_version"]),
        ingested_at=r["ingested_at"],
    )


__all__ = ["ClickHouseBackend", "parse_dsn"]
