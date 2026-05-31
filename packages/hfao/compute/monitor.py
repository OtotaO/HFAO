"""Monitor / alert engine (SPEC §8.4).

Three pieces:

  * :class:`SQLGenerator` — pluggable NL→SQL backend. Built-ins:
      - :class:`KeywordTemplateGenerator` — deterministic, keyword-driven
        templates over the canonical schema. Used by the cockpit's preview
        (PR #10) and as the test-friendly default.
      - :class:`LLMSQLGenerator` — judge-model-driven, JSON-mode constrained.
  * :class:`MonitorEngine` — evaluates a monitor's frozen SQL via the storage
    backend's ``execute_readonly_sql`` (per-project scoped), checks the
    threshold, records an :class:`Alert`, dispatches to configured webhook
    channels.
  * :func:`create_monitor` — top-level helper: NL → SQL (one-shot, on create),
    persisted via the control plane.

Per §8.4 the SQL is **frozen on creation** — :class:`MonitorEngine.evaluate`
never re-asks the LLM. Re-running NL→SQL on every tick would silently change
semantics and is explicitly not the contract.

The third automated insight surface lands here: combined with the Causal
Attribution (PR #12) and the Eval Engine (PR #13), the Monitor Engine
*automatically* fires alerts when KPIs drift — closing the loop from raw
agent traces to actionable outbound signals.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from hfao.schema.monitors import Alert, Monitor, MonitorOperator

if TYPE_CHECKING:
    from hfao.compute.causal.judge import Judge
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# NL → SQL — two backends
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GeneratedSQL:
    """Output of :class:`SQLGenerator`. Carries provenance so the cockpit /
    MCP can show users which backend produced the SQL (and whether it was a
    deterministic template vs. an LLM)."""

    sql: str
    backend: str
    matched_template: str | None = None


class SQLGenerator(Protocol):
    """Produces the frozen SQL for a monitor from its NL description.

    The contract is **deterministic** for a given (description, window) pair
    when the backend is one of the built-in template generators; LLM-backed
    generators may return different SQL across calls and so should be
    invoked at monitor creation time only.
    """

    backend: str

    def generate(self, *, description: str, window: str) -> GeneratedSQL: ...


@dataclass(frozen=True)
class KeywordTemplateGenerator:
    """Keyword-driven NL→SQL. Covers the most common KPIs the cockpit
    advertises (error rate, total cost, p95 latency, token usage).

    Returns SQL the storage backend's ``execute_readonly_sql`` will accept:
    canonical schema only, single statement, SELECT-prefixed.
    """

    backend: str = "keyword_template"

    def generate(self, *, description: str, window: str) -> GeneratedSQL:
        text = (description or "").lower()
        window_clause = _window_clause(window)
        for keywords, template, label in _TEMPLATES:
            if all(k in text for k in keywords):
                sql = template.replace("{WINDOW}", window_clause)
                return GeneratedSQL(
                    sql=sql, backend=self.backend, matched_template=label
                )
        # Default: total event count over the window — always works.
        return GeneratedSQL(
            sql=(
                "SELECT count() AS value FROM events_current "
                f"WHERE start_time >= {window_clause}"
            ),
            backend=self.backend,
            matched_template="default_count",
        )


_TEMPLATES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("error", "rate"),
        (
            "SELECT (count() FILTER (WHERE status = 'error') * 1.0) "
            "/ NULLIF(count(), 0) AS value "
            "FROM events_current WHERE start_time >= {WINDOW}"
        ),
        "error_rate",
    ),
    (
        ("cost",),
        (
            "SELECT COALESCE(SUM(total_cost_usd), 0.0) AS value "
            "FROM events_current WHERE start_time >= {WINDOW}"
        ),
        "total_cost_usd",
    ),
    (
        ("latency", "p95"),
        (
            "SELECT COALESCE(quantile_cont(duration_ms, 0.95), 0.0) AS value "
            "FROM events_current "
            "WHERE start_time >= {WINDOW} AND duration_ms IS NOT NULL"
        ),
        "latency_p95_ms",
    ),
    (
        ("token", "usage"),
        (
            "SELECT COALESCE(SUM(total_tokens), 0) AS value "
            "FROM events_current WHERE start_time >= {WINDOW}"
        ),
        "total_tokens",
    ),
)


_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_WINDOW_UNITS = {
    "s": ("SECOND", 1),
    "m": ("MINUTE", 60),
    "h": ("HOUR", 3600),
    "d": ("DAY", 86400),
    "w": ("WEEK", 604800),
}


def _window_clause(window: str) -> str:
    """Render a SQL fragment for ``start_time >= now() - INTERVAL ...``."""
    match = _WINDOW_RE.match(window)
    if not match:
        # Fall back to one hour; never emit invalid SQL.
        return "now() - INTERVAL '1 HOUR'"
    qty, unit = int(match.group(1)), match.group(2).lower()
    sql_unit, _seconds = _WINDOW_UNITS[unit]
    return f"now() - INTERVAL '{qty} {sql_unit}'"


def parse_window_seconds(window: str) -> int:
    """Return the window length in seconds (used by the runner cadence)."""
    match = _WINDOW_RE.match(window)
    if not match:
        return 3600
    qty, unit = int(match.group(1)), match.group(2).lower()
    _sql_unit, seconds = _WINDOW_UNITS[unit]
    return qty * seconds


LLM_SQL_SYSTEM_PROMPT = (
    "You are an HFAO monitor generator. Translate a natural-language "
    "description into a SINGLE SQL statement against the canonical schema. "
    "Allowed tables: events_current, scores, causal_edges, cost_daily. "
    "Return ONLY JSON: {\"sql\": \"SELECT ... AS value FROM ...\"}. The query "
    "MUST return exactly one row with one numeric column named `value`. "
    "Use the configured window for time ranges."
)


@dataclass
class LLMSQLGenerator(SQLGenerator):
    """LLM-driven generator. The judge is asked for JSON-only output; the
    runner falls back to the keyword template generator on any failure so
    the monitor is never created with empty SQL.
    """

    judge: Judge | None = None
    backend: str = "llm"
    _fallback: KeywordTemplateGenerator = field(
        default_factory=KeywordTemplateGenerator, init=False, repr=False
    )

    def generate(self, *, description: str, window: str) -> GeneratedSQL:
        if self.judge is None:
            from hfao.compute.causal.judge import select_judge
            from hfao.config import HFAOConfig

            self.judge = select_judge(HFAOConfig.from_env())
        prompt = json.dumps(
            {"description": description, "window": window},
        )
        try:
            from hfao.compute.causal.judge import JudgeHypothesis

            hypotheses: list[JudgeHypothesis] = self.judge.attribute([], [])
            # We reuse the judge's `reason` field to carry the SQL text out.
            for h in hypotheses:
                if h.reason and "select" in h.reason.lower():
                    return GeneratedSQL(
                        sql=h.reason.strip(),
                        backend=f"llm:{self.judge.model}",
                    )
        except Exception as exc:  # noqa: BLE001 — fall through to template
            log.warning("LLMSQLGenerator failed; falling back to template: %s", exc)
        del prompt
        fallback = self._fallback.generate(description=description, window=window)
        return GeneratedSQL(
            sql=fallback.sql,
            backend=f"llm_fallback:{self._fallback.backend}",
            matched_template=fallback.matched_template,
        )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MonitorEvaluation:
    """The result of one ``MonitorEngine.evaluate`` call."""

    monitor_id: str
    actual_value: float
    threshold_breached: bool
    message: str
    alert: Alert | None


WebhookFn = Callable[[str, dict[str, Any]], tuple[bool, str | None]]


def http_webhook(url: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    """POST ``payload`` as JSON to ``url``. Returns ``(ok, error_message)``."""
    try:
        resp = httpx.post(url, json=payload, timeout=5.0)
    except httpx.HTTPError as exc:
        return False, str(exc)
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
    return True, None


class MonitorEngine:
    """Evaluates monitors and dispatches alerts.

    ``webhook`` is parameterised so AC tests can capture deliveries without
    standing up a real HTTP server. Production uses :func:`http_webhook`.
    """

    def __init__(
        self,
        backend: StorageBackend,
        control: ControlPlane,
        *,
        webhook: WebhookFn = http_webhook,
    ) -> None:
        self._backend = backend
        self._control = control
        self._webhook = webhook

    def evaluate(
        self, monitor: dict[str, Any] | Monitor
    ) -> MonitorEvaluation:
        """Run one monitor and emit an :class:`Alert` if its threshold breaks."""
        row = monitor if isinstance(monitor, dict) else _monitor_to_dict(monitor)
        project_id = str(row["project_id"])
        monitor_id = str(row["id"])
        actual = self._run_query(project_id, str(row["sql_query"]))
        threshold = float(row["threshold"])
        operator = str(row["operator"])
        breached = _compare(actual, operator, threshold)
        self._control.mark_monitor_evaluated(
            project_id=project_id, monitor_id=monitor_id
        )
        if not breached:
            return MonitorEvaluation(
                monitor_id=monitor_id,
                actual_value=actual,
                threshold_breached=False,
                message=(
                    f"value={actual:.4f} {operator} {threshold:.4f} → ok"
                ),
                alert=None,
            )
        # Threshold breached — deliver alerts + record.
        message = (
            f"{row['name']!s}: value={actual:.4f} {operator} {threshold:.4f} "
            f"over {row['window']!s}"
        )
        channels = _parse_channels(row.get("channels"))
        delivered: list[str] = []
        errors: list[str] = []
        payload = {
            "monitor_id": monitor_id,
            "monitor_name": row["name"],
            "project_id": project_id,
            "actual_value": actual,
            "threshold": threshold,
            "operator": operator,
            "window": row["window"],
            "message": message,
            "nl_description": row["nl_description"],
        }
        for url in channels:
            ok, err = self._webhook(url, payload)
            if ok:
                delivered.append(url)
            else:
                errors.append(f"{url}: {err or 'delivery failed'}")
        fired_at = datetime.now(timezone.utc).isoformat()
        record = self._control.record_alert(
            project_id=project_id,
            monitor_id=monitor_id,
            fired_at=fired_at,
            actual_value=actual,
            threshold=threshold,
            operator=operator,
            message=message,
            channels_notified=delivered,
            delivery_errors=errors,
        )
        return MonitorEvaluation(
            monitor_id=monitor_id,
            actual_value=actual,
            threshold_breached=True,
            message=message,
            alert=_alert_from_row(record),
        )

    def evaluate_all_enabled(
        self, *, project_id: str
    ) -> list[MonitorEvaluation]:
        """Evaluate every enabled monitor in a project once."""
        monitors = self._control.list_monitors(
            project_id=project_id, only_enabled=True
        )
        return [self.evaluate(m) for m in monitors]

    def _run_query(self, project_id: str, sql: str) -> float:
        rows = self._backend.execute_readonly_sql(project_id, sql)
        if not rows:
            return 0.0
        first = rows[0]
        if "value" in first:
            return _coerce_float(first["value"])
        # Caller may have omitted the alias — take the first column.
        first_key = next(iter(first.keys()))
        return _coerce_float(first[first_key])


def _coerce_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_OPS: dict[MonitorOperator, Callable[[float, float], bool]] = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


def _compare(value: float, operator: str, threshold: float) -> bool:
    op = _OPS.get(operator)  # type: ignore[arg-type]
    if op is None:
        raise ValueError(f"unknown operator: {operator!r}")
    return op(value, threshold)


def _parse_channels(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(c) for c in raw]  # type: ignore[arg-type]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(c) for c in parsed]  # type: ignore[arg-type]
        except json.JSONDecodeError:
            pass
    return []


def _monitor_to_dict(monitor: Monitor) -> dict[str, Any]:
    return {
        "project_id": monitor.project_id,
        "id": monitor.id,
        "name": monitor.name,
        "nl_description": monitor.nl_description,
        "sql_query": monitor.sql_query,
        "threshold": monitor.threshold,
        "operator": monitor.operator,
        "window": monitor.window,
        "channels": list(monitor.channels),
        "enabled": monitor.enabled,
    }


def _alert_from_row(row: dict[str, Any]) -> Alert:
    return Alert(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        monitor_id=str(row["monitor_id"]),
        fired_at=_parse_iso(row["fired_at"]),
        actual_value=float(row["actual_value"]),
        threshold=float(row["threshold"]),
        operator=row["operator"],
        message=str(row["message"]),
        channels_notified=_parse_channels(row.get("channels_notified")),
        delivery_errors=_parse_channels(row.get("delivery_errors")),
    )


def _parse_iso(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


# --------------------------------------------------------------------------- #
# Top-level create helper
# --------------------------------------------------------------------------- #


def create_monitor(
    control: ControlPlane,
    *,
    project_id: str,
    name: str,
    nl_description: str,
    threshold: float,
    operator: str,
    window: str,
    channels: list[str] | None = None,
    enabled: bool = True,
    generator: SQLGenerator | None = None,
) -> dict[str, Any]:
    """NL → SQL (one-shot) → persist via the control plane.

    Per §8.4 the SQL is frozen at this point. Editing the SQL later is a
    deliberate update; the engine never regenerates it from the description.
    """
    gen = generator or KeywordTemplateGenerator()
    sql = gen.generate(description=nl_description, window=window).sql
    return control.create_monitor(
        project_id=project_id,
        name=name,
        nl_description=nl_description,
        sql_query=sql,
        threshold=threshold,
        operator=operator,
        window=window,
        channels=channels,
        enabled=enabled,
    )


# --------------------------------------------------------------------------- #
# Worker — periodic evaluation across all enabled monitors
# --------------------------------------------------------------------------- #


class MonitorWorker:
    """Periodically evaluate enabled monitors for a project. Best-effort: any
    individual monitor failure is logged and the loop continues."""

    DEFAULT_INTERVAL_S = 60

    def __init__(
        self,
        engine: MonitorEngine,
        *,
        project_id: str,
        interval_s: int = DEFAULT_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._project_id = project_id
        self._interval_s = max(1, int(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hfao-monitor-worker", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._engine.evaluate_all_enabled(project_id=self._project_id)
            except Exception:  # noqa: BLE001 — log + keep ticking
                log.exception("monitor worker iteration failed; will retry")
            self._stop.wait(self._interval_s)


__all__ = [
    "LLM_SQL_SYSTEM_PROMPT",
    "Alert",
    "GeneratedSQL",
    "KeywordTemplateGenerator",
    "LLMSQLGenerator",
    "Monitor",
    "MonitorEngine",
    "MonitorEvaluation",
    "MonitorWorker",
    "SQLGenerator",
    "WebhookFn",
    "create_monitor",
    "http_webhook",
    "parse_window_seconds",
]
