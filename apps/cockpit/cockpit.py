"""HFAO Cockpit — Gradio 6 single-file UI.

SPEC §10. All 12 §10.2 tabs ship in Week 6:

    Home · Traces · Trace detail · Live tail · Datasets · Prompts · Evals ·
    Annotations · Monitors · Costs · Settings · Ask HFAO

Imports from ``hfao.*`` only — no SQL leaks outside ``packages/hfao/storage``
(Appendix C rule 4).

Launch:

    python -m hfao.cli up
    # → apps.cockpit.cockpit:demo.launch(mcp_server=True, server_port=7860)

Every read-side handler is registered with ``api_name="cockpit.read.*"`` so it
auto-exports as a Gradio MCP tool when ``mcp_server=True`` (§10.4). Write-side
handlers use ``cockpit.write.*``.

Two §15.2 cross-week deferrals (resolved §16 Q-17) are surfaced honestly:

- **Evals** tab renders past eval runs read-only from the ``scores`` table
  (grouped by ``eval_run_id``); launching an eval lands in Week 8 with the
  experiment runner.
- **Monitors** tab renders the configured monitors list read-only; the NL→SQL
  preview is a keyword-template stub that the Week 7 monitor engine replaces.

Both tabs say so explicitly in the UI; neither silently fakes engine output.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Iterator
from typing import Any, cast

import gradio as gr
import msgspec
from hfao.config import HFAOConfig
from hfao.schema.events import Observation
from hfao.storage import StorageBackend
from hfao.storage.clickhouse_backend import ClickHouseBackend
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend

from apps.cockpit.components import live_tail, span_tree, trace_chat

CONFIG = HFAOConfig.from_env()
DEFAULT_PROJECT = CONFIG.project
COCKPIT_ACTOR = "cockpit"
DEFAULT_WORKSPACE_SLUG = "default"


# ---- backend lifecycle ---------------------------------------------------


@contextlib.contextmanager
def _open_backend() -> Iterator[StorageBackend]:
    """Open the configured backend for a single handler invocation.

    Per Appendix A: ``HFAO_BACKEND=duckdb`` (default) → DuckDB at
    ``HFAO_DUCKDB_PATH``; ``HFAO_BACKEND=clickhouse`` → ClickHouse via
    ``HFAO_CLICKHOUSE_DSN``.
    """
    if CONFIG.backend == "clickhouse":
        if not CONFIG.clickhouse_dsn:
            raise RuntimeError("HFAO_BACKEND=clickhouse requires HFAO_CLICKHOUSE_DSN")
        backend = ClickHouseBackend(CONFIG.clickhouse_dsn)
        backend.init_schema()
        try:
            yield backend
        finally:
            backend.close()
        return
    path = CONFIG.duckdb_path
    if path == "/data/hfao.duckdb":
        # Single-binary default needs root; fall back to cwd-relative
        # like the demo CLI does (matches §15.2 / hfao/cli.py).
        from pathlib import Path

        path = str(Path.cwd() / "hfao.duckdb")
    duck = DuckDBBackend(path)
    duck.init_schema()
    try:
        yield duck
    finally:
        duck.close()


# ---- read handlers (api_name="cockpit.read.*") ---------------------------


def cockpit_recent_traces(project: str, limit: int = 20) -> list[dict[str, Any]]:
    """Most-recent N traces for the project, list_traces shape."""
    project = project or DEFAULT_PROJECT
    with _open_backend() as backend:
        return backend.list_traces(project, limit=limit)


def cockpit_quick_stats(project: str) -> dict[str, Any]:
    """Home tab quick-stats card payload."""
    project = project or DEFAULT_PROJECT
    with _open_backend() as backend:
        recent = backend.list_traces(project, limit=200)
    span_count = sum(int(r.get("span_count") or 0) for r in recent)
    error_count = sum(1 for r in recent if r.get("has_error"))
    cost = sum(float(r.get("total_cost_usd") or 0.0) for r in recent)
    return {
        "trace_count": len(recent),
        "span_count": span_count,
        "error_rate": (error_count / len(recent)) if recent else 0.0,
        "total_cost_usd": cost,
    }


def cockpit_traces_table(
    project: str,
    where_sql: str = "1=1",
    limit: int = 200,
) -> list[list[Any]]:
    """Traces tab dataframe rows (capped at 5K per §10.2)."""
    project = project or DEFAULT_PROJECT
    capped = min(limit, 5000)
    with _open_backend() as backend:
        rows = backend.list_traces(project, where_sql=where_sql or "1=1", limit=capped)
    return [
        [
            str(r.get("trace_id", "")),
            int(r.get("span_count") or 0),
            "error" if r.get("has_error") else "ok",
            int(r.get("total_tokens") or 0),
            f"${float(r.get('total_cost_usd') or 0.0):.4f}",
            str(r.get("session_id") or ""),
            str(r.get("first_start") or ""),
            str(r.get("last_end") or ""),
        ]
        for r in rows
    ]


def cockpit_trace_detail(project: str, trace_id: str) -> dict[str, Any]:
    """Trace detail payload: span tree + chat messages + scores + edges."""
    project = project or DEFAULT_PROJECT
    if not trace_id:
        return _empty_trace_payload()
    with _open_backend() as backend:
        observations = backend.get_trace(project, trace_id)
        scores = backend.get_scores(project, trace_id)
        edges = backend.get_causal_edges(project, trace_id)
    obs_dicts = [_obs_to_dict(o) for o in observations]
    return {
        "tree_html": span_tree.render(obs_dicts),
        "chat_messages": trace_chat.build_messages(obs_dicts),
        "scores_table": [
            [s.name, s.value, s.string_value or "", s.source, s.judge_model or ""]
            for s in scores
        ],
        "edges_table": [
            [
                e.source_observation_id,
                e.target_observation_id,
                e.edge_type,
                f"{e.confidence:.2f}",
                e.method,
                "yes" if e.replay_supported else "no",
            ]
            for e in edges
        ],
        "summary": _trace_summary(obs_dicts),
    }


def cockpit_live_tail(project: str) -> str:
    """Live tail tab HTML — last 20 traces, status pills."""
    project = project or DEFAULT_PROJECT
    with _open_backend() as backend:
        rows = backend.list_traces(project, limit=20)
    return live_tail.render(rows)


# ---- control-plane lifecycle ---------------------------------------------


@contextlib.contextmanager
def _open_control_plane() -> Iterator[ControlPlane]:
    """Open the control plane for a single handler invocation."""
    cp = ControlPlane(CONFIG.control_plane_dsn)
    cp.init_schema()
    try:
        yield cp
    finally:
        cp.close()


def _ensure_project(cp: ControlPlane, project: str) -> str:
    """Resolve ``project`` to a control-plane ``projects.id``.

    The Home/Traces tabs accept any string as a project label and pass it
    straight to the backend (events store the project_id as a free string).
    The Week-6 control-plane tabs (Datasets/Prompts/Annotations) need a real
    ``projects`` row because the FK is enforced by the SQLite DDL. To keep the
    single-binary UX seamless this helper:

      1. Returns the ``projects.id`` if a row already exists with that id.
      2. Otherwise creates a ``default`` workspace if missing and a project
         whose ``id`` equals the given string (so subsequent backend rows
         keyed on that same string stay joined). Returns the id.

    Idempotent on repeat calls.
    """
    try:
        cp.get_project(project)
        return project
    except KeyError:
        pass
    ws_row = cp.get_workspace_by_slug(DEFAULT_WORKSPACE_SLUG)
    if ws_row is None:
        ws_row = cp.create_workspace(slug=DEFAULT_WORKSPACE_SLUG, name="Default")
    # Insert with the caller's id literally so events written under that same
    # project string remain referentially intact across cockpit lifetimes.
    cp.create_project_with_id(
        project_id=project, workspace_id=ws_row["id"], slug=project, name=project
    )
    return project


# ---- Week 6 tab handlers --------------------------------------------------
# Datasets (§10.2 tab 5)


def cockpit_datasets_list(project: str) -> list[list[Any]]:
    """Rows for the Datasets tab table."""
    project = project or DEFAULT_PROJECT
    with _open_control_plane() as cp:
        try:
            cp.get_project(project)
        except KeyError:
            return []
        rows = cp.list_datasets(project_id=project)
    return [[r["id"], r["name"], r.get("description") or "", r["created_at"]] for r in rows]


def cockpit_create_dataset(
    project: str, name: str, description: str = ""
) -> dict[str, Any]:
    """Create a dataset under ``project``. Idempotent project bootstrap."""
    project = project or DEFAULT_PROJECT
    name = (name or "").strip()
    if not name:
        raise ValueError("dataset name is required")
    with _open_control_plane() as cp:
        pid = _ensure_project(cp, project)
        ds = cp.create_dataset(
            project_id=pid, name=name, description=description or None
        )
        cp.record_audit(
            workspace_id=cp.get_project(pid)["workspace_id"],
            actor=COCKPIT_ACTOR,
            action="create_dataset",
            target=f"{pid}/{ds['id']}",
            details=json.dumps({"name": name}),
        )
    return ds


def cockpit_add_dataset_item_from_trace(
    project: str,
    dataset_id: str,
    trace_id: str,
    observation_id: str | None = None,
) -> dict[str, Any]:
    """"Add to dataset" wired from Trace detail (§10.2 tab 5)."""
    project = project or DEFAULT_PROJECT
    trace_id = (trace_id or "").strip()
    if not trace_id:
        raise ValueError("trace_id is required")
    with _open_backend() as backend:
        observations = backend.get_trace(project, trace_id)
    if not observations:
        raise ValueError(f"trace not found: {trace_id}")
    chosen = _pick_observation(observations, observation_id)
    payload_input = chosen.input or ""
    payload_output = chosen.output or ""
    metadata = {
        "trace_id": trace_id,
        "observation_id": chosen.observation_id,
        "model": chosen.model or "",
        "name": chosen.name,
    }
    with _open_control_plane() as cp:
        item = cp.add_dataset_item(
            project_id=project,
            dataset_id=dataset_id,
            input=payload_input,
            expected_output=payload_output or None,
            metadata=metadata,
            source_trace_id=trace_id,
            source_observation_id=chosen.observation_id,
        )
        cp.record_audit(
            workspace_id=cp.get_project(project)["workspace_id"],
            actor=COCKPIT_ACTOR,
            action="add_dataset_item",
            target=f"{project}/{dataset_id}/{item['id']}",
            details=json.dumps({"trace_id": trace_id}),
        )
    return item


def _pick_observation(
    observations: list[Observation], observation_id: str | None
) -> Observation:
    if observation_id:
        for o in observations:
            if o.observation_id == observation_id:
                return o
        raise ValueError(f"observation {observation_id} not in trace")
    for o in observations:
        if o.parent_observation_id is None:
            return o
    return observations[0]


# Prompts (§10.2 tab 6)


def cockpit_prompts_list(project: str) -> list[list[Any]]:
    """Latest version of every prompt in the project."""
    project = project or DEFAULT_PROJECT
    with _open_control_plane() as cp:
        try:
            cp.get_project(project)
        except KeyError:
            return []
        rows = cp.list_prompts(project_id=project)
    return [
        [r["name"], int(r["version"]), r["type"], r["created_by"], r["created_at"]]
        for r in rows
    ]


def cockpit_create_prompt(
    project: str,
    name: str,
    content: str,
    type: str = "text",  # noqa: A002 — matches §4.1 PromptVersion.type
    label: str = "",
    created_by: str = COCKPIT_ACTOR,
    commit_message: str = "",
) -> dict[str, Any]:
    """Create a new prompt version; optionally move ``label`` to it."""
    project = project or DEFAULT_PROJECT
    name = (name or "").strip()
    if not name:
        raise ValueError("prompt name is required")
    if type not in ("text", "chat"):
        raise ValueError("type must be 'text' or 'chat'")
    with _open_control_plane() as cp:
        pid = _ensure_project(cp, project)
        version = cp.create_prompt_version(
            project_id=pid,
            name=name,
            type=type,
            content=content,
            created_by=created_by,
            commit_message=commit_message or None,
        )
        workspace_id = cp.get_project(pid)["workspace_id"]
        cp.record_audit(
            workspace_id=workspace_id,
            actor=created_by,
            action="create_prompt_version",
            target=f"{pid}/{name}@v{version['version']}",
            details=json.dumps({"commit_message": commit_message}),
        )
        if label:
            cp.set_prompt_label(
                project_id=pid, name=name, label=label, version=version["version"]
            )
            cp.record_audit(
                workspace_id=workspace_id,
                actor=created_by,
                action="set_prompt_label",
                target=f"{pid}/{name}/{label}",
                details=json.dumps({"version": version["version"]}),
            )
    return version


def cockpit_set_prompt_label(
    project: str, name: str, label: str, version: int, actor: str = COCKPIT_ACTOR
) -> dict[str, Any]:
    """Move ``label`` to ``version``. Records an audit-log entry (§13.5)."""
    project = project or DEFAULT_PROJECT
    name = (name or "").strip()
    label = (label or "").strip()
    if not (name and label):
        raise ValueError("prompt name and label are required")
    with _open_control_plane() as cp:
        cp.set_prompt_label(
            project_id=project, name=name, label=label, version=int(version)
        )
        workspace_id = cp.get_project(project)["workspace_id"]
        cp.record_audit(
            workspace_id=workspace_id,
            actor=actor,
            action="set_prompt_label",
            target=f"{project}/{name}/{label}",
            details=json.dumps({"version": int(version)}),
        )
        prompt = cp.get_prompt(project_id=project, name=name, label=label)
    if prompt is None:
        raise ValueError(f"prompt {name!r} has no version {version}")
    return prompt


# Evals (§10.2 tab 7) — read-only over scores until Week 8 runner lands.


def cockpit_evals_list(project: str) -> list[list[Any]]:
    """Past eval runs grouped from ``scores.eval_run_id``."""
    project = project or DEFAULT_PROJECT
    with _open_backend() as backend:
        try:
            rows = backend.execute_readonly_sql(
                project,
                "SELECT eval_run_id, "
                "       count() AS scores, "
                "       avg(value) AS mean_value, "
                "       min(timestamp) AS started_at, "
                "       max(timestamp) AS finished_at "
                "FROM scores "
                "WHERE eval_run_id IS NOT NULL AND eval_run_id <> '' "
                "GROUP BY eval_run_id "
                "ORDER BY started_at DESC",
            )
        except Exception:  # noqa: BLE001 — backend not ready / empty
            rows = []
    return [
        [
            r["eval_run_id"],
            int(r["scores"]),
            float(r.get("mean_value") or 0.0),
            str(r["started_at"]),
            str(r["finished_at"]),
        ]
        for r in rows
    ]


EVALS_DEFER_NOTICE = (
    "Eval launching lands in **Week 8** with the experiment runner "
    "(§15.2; resolved §16 Q-17). This tab is read-only for now: it lists past "
    "eval runs by `eval_run_id` from the `scores` table once any exist."
)


# Annotations (§10.2 tab 8)


def cockpit_annotation_queues_list(project: str) -> list[list[Any]]:
    project = project or DEFAULT_PROJECT
    with _open_control_plane() as cp:
        try:
            cp.get_project(project)
        except KeyError:
            return []
        queues = cp.list_annotation_queues(project_id=project)
    return [[q["id"], q["name"], q["filter_query"], q["created_at"]] for q in queues]


def cockpit_create_annotation_queue(
    project: str,
    name: str,
    filter_query: str = "1=1",
    score_schema: str = "",
) -> dict[str, Any]:
    """Create an annotation queue. ``score_schema`` is comma-separated names."""
    project = project or DEFAULT_PROJECT
    name = (name or "").strip()
    if not name:
        raise ValueError("queue name is required")
    schema = [s.strip() for s in score_schema.split(",") if s.strip()]
    with _open_control_plane() as cp:
        pid = _ensure_project(cp, project)
        queue = cp.create_annotation_queue(
            project_id=pid, name=name, filter_query=filter_query, score_schema=schema
        )
        cp.record_audit(
            workspace_id=cp.get_project(pid)["workspace_id"],
            actor=COCKPIT_ACTOR,
            action="create_annotation_queue",
            target=f"{pid}/{queue['id']}",
            details=json.dumps({"filter_query": filter_query, "schema": schema}),
        )
    return queue


# Monitors (§10.2 tab 9) — read-only list until Week 7 engine lands.


def cockpit_monitors_list(project: str) -> list[list[Any]]:  # noqa: ARG001
    """No monitor table yet; engine lands in Week 7 (§15.2). Returns []."""
    return []


_MONITOR_TEMPLATES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("error", "rate"),
        "SELECT count() FILTER (WHERE status = 'error') * 1.0 / count() AS error_rate "
        "FROM events_current WHERE start_time >= now() - INTERVAL '{window}'",
    ),
    (
        ("cost",),
        "SELECT sum(total_cost_usd) AS total_cost_usd "
        "FROM events_current WHERE start_time >= now() - INTERVAL '{window}'",
    ),
    (
        ("latency", "p95"),
        "SELECT quantile_cont(duration_ms, 0.95) AS p95_latency_ms "
        "FROM events_current WHERE start_time >= now() - INTERVAL '{window}'",
    ),
    (
        ("token", "usage"),
        "SELECT sum(total_tokens) AS total_tokens "
        "FROM events_current WHERE start_time >= now() - INTERVAL '{window}'",
    ),
)


def cockpit_monitor_nl_preview(nl: str, window: str = "1 hour") -> str:
    """Keyword-template NL→SQL preview.

    The Week 7 monitor engine (``hfao.compute.monitor``) replaces this with a
    judge-model-driven NL→SQL pass; this stub exists so the Monitors tab
    has a visible preview surface in Week 6. The returned string carries an
    explicit ``-- STUB --`` marker so it never gets mistaken for the real
    engine's output.
    """
    text = (nl or "").lower()
    for keywords, template in _MONITOR_TEMPLATES:
        if all(k in text for k in keywords):
            return f"-- STUB (Week 7 engine replaces this)\n{template.format(window=window)}"
    return (
        "-- STUB (Week 7 engine replaces this)\n"
        "-- No keyword template matched. Try: 'error rate', 'cost', "
        "'latency p95', 'token usage'."
    )


MONITORS_DEFER_NOTICE = (
    "The monitor engine lands in **Week 7** (§15.2; resolved §16 Q-17). This "
    "tab is read-only for now and the NL→SQL preview below is a keyword-template "
    "stub clearly marked with `-- STUB --` so it can't be mistaken for the real "
    "engine's output."
)


# Costs (§10.2 tab 10)


def cockpit_cost_rollup(
    project: str, group_by: list[str] | None = None, window: str = "7d"
) -> list[list[Any]]:
    """Pivot table rows for the Costs tab."""
    from datetime import datetime, timezone

    project = project or DEFAULT_PROJECT
    group_by = group_by or ["model"]
    now = datetime.now(timezone.utc)
    delta = _parse_cockpit_window(window)
    with _open_backend() as backend:
        rows = backend.cost_rollup(
            project, date_from=now - delta, date_to=now, group_by=group_by
        )
    header_cols = list(group_by)
    out: list[list[Any]] = []
    for r in rows:
        row: list[Any] = [r.get(c, "") for c in header_cols]
        row.extend(
            [
                float(r.get("total_cost_usd") or 0.0),
                int(r.get("total_tokens") or 0),
                int(r.get("call_count") or 0),
            ]
        )
        out.append(row)
    return out


def _parse_cockpit_window(window: str):  # type: ignore[no-untyped-def]
    """Local copy of the MCP window parser to keep cockpit self-contained."""
    from datetime import timedelta

    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    text = (window or "7d").strip().lower()
    if not text or text[-1] not in units:
        raise ValueError(f"invalid window: {window!r}")
    return timedelta(**{units[text[-1]]: int(text[:-1])})


# Settings (§10.2 tab 11)


def cockpit_settings_payload() -> dict[str, Any]:
    """Snapshot of effective config + control-plane keys for the Settings tab."""
    keys: list[dict[str, Any]] = []
    with _open_control_plane() as cp:
        for ws in cp.list_workspaces():
            keys.extend(cp.list_api_keys(workspace_id=ws["id"]))
    return {
        "project": CONFIG.project,
        "environment": CONFIG.environment,
        "backend": CONFIG.backend,
        "duckdb_path": CONFIG.duckdb_path,
        "control_plane_dsn": CONFIG.control_plane_dsn,
        "redaction_profile": CONFIG.redaction_profile,
        "judge_provider": CONFIG.judge_provider,
        "judge_model": CONFIG.judge_model,
        "mcp_read_only": CONFIG.mcp_read_only,
        "api_keys": [
            {
                "id": k["id"],
                "prefix": k["prefix"],
                "role": k["role"],
                "name": k["name"],
                "created_at": k["created_at"],
                "revoked": k.get("revoked_at") is not None,
            }
            for k in keys
        ],
    }


# Ask HFAO (§10.2 tab 12) — local copilot router over the §9 MCP surface.


def cockpit_ask_hfao(project: str, question: str) -> str:
    """Route a question to the relevant §9 MCP tool and return a grounded answer.

    A deterministic intent router for v1: the goal is a ``gr.ChatInterface``
    handler that always returns *grounded* output backed by a real MCP read tool
    (§9.2). A judge-model-driven orchestrator can replace this body later
    without breaking the cockpit API.
    """
    project = project or DEFAULT_PROJECT
    text = (question or "").strip()
    if not text:
        return "Ask something like 'recent traces', 'failures', 'cost', or "
        "'trace <id>'."
    lower = text.lower()
    backend = None
    try:
        with _open_backend() as backend:
            if any(k in lower for k in ("fail", "error", "decisive")):
                rows = backend.list_traces(
                    project, where_sql="status = 'error'", limit=10
                )
                if not rows:
                    return f"No error traces found in **{project}** in the recent window."
                return _format_trace_rows(
                    f"Most recent failing traces in **{project}**:", rows
                )
            if "cost" in lower:
                pivot = cockpit_cost_rollup(project, ["model"], "7d")
                if not pivot:
                    return f"No cost data yet for **{project}**."
                lines = ["Cost by model (last 7d):"]
                for row in pivot:
                    model, cost, tokens, calls = row[0], row[1], row[2], row[3]
                    lines.append(
                        f"- `{model or '∅'}`: ${cost:.4f} · {tokens} tok · {calls} calls"
                    )
                return "\n".join(lines)
            if lower.startswith("trace "):
                trace_id = text.split(" ", 1)[1].strip()
                observations = backend.get_trace(project, trace_id)
                if not observations:
                    return f"No trace `{trace_id}` in **{project}**."
                edges = backend.get_causal_edges(project, trace_id)
                has_err = any(o.status == "error" for o in observations)
                lines = [
                    f"Trace `{trace_id}` in **{project}**: "
                    f"{len(observations)} spans, "
                    f"status={'error' if has_err else 'ok'}.",
                ]
                if edges:
                    lines.append(
                        "Causal hypotheses (not verdicts — weigh by confidence):"
                    )
                    for e in sorted(edges, key=lambda e: e.confidence, reverse=True)[:5]:
                        lines.append(
                            f"- {e.edge_type} (conf={e.confidence:.2f}, "
                            f"method={e.method}, replay={'yes' if e.replay_supported else 'no'}): "
                            f"{e.evidence}"
                        )
                return "\n".join(lines)
            # Default: list recent traces
            rows = backend.list_traces(project, limit=10)
            if not rows:
                return f"No traces yet in **{project}**."
            return _format_trace_rows(
                f"Most recent traces in **{project}**:", rows
            )
    except Exception as exc:  # noqa: BLE001 - return a chat-friendly error
        return f"Sorry, that lookup failed: `{exc!s}`"


def _format_trace_rows(header: str, rows: list[dict[str, Any]]) -> str:
    lines = [header]
    for r in rows[:10]:
        tid = r.get("trace_id", "")
        spans = int(r.get("span_count") or 0)
        cost = float(r.get("total_cost_usd") or 0.0)
        status = "error" if r.get("has_error") else "ok"
        lines.append(f"- `{tid}` · {spans} spans · {status} · ${cost:.4f}")
    return "\n".join(lines)


# ---- helpers --------------------------------------------------------------


def _obs_to_dict(obs: Observation) -> dict[str, Any]:
    return cast("dict[str, Any]", msgspec.to_builtins(obs))


def _empty_trace_payload() -> dict[str, Any]:
    return {
        "tree_html": span_tree.render([]),
        "chat_messages": [(None, "_(select a trace from the Traces tab)_")],
        "scores_table": [],
        "edges_table": [],
        "summary": "no trace selected",
    }


def _trace_summary(observations: Iterable[dict[str, Any]]) -> str:
    obs_list = list(observations)
    if not obs_list:
        return "no observations"
    spans = len(obs_list)
    total_tokens = 0
    total_cost = 0.0
    for o in obs_list:
        usage = o.get("usage") or {}
        if isinstance(usage, dict):
            total_tokens += int(usage.get("total_tokens") or 0)
        cost = o.get("cost") or {}
        if isinstance(cost, dict):
            total_cost += float(cost.get("total_cost_usd") or 0.0)
    return f"{spans} spans · {total_tokens} tokens · ${total_cost:.4f}"


# ---- UI -------------------------------------------------------------------


_TRACES_HEADERS = [
    "trace_id",
    "spans",
    "status",
    "tokens",
    "cost",
    "session",
    "first_start",
    "last_end",
]


def build_blocks() -> gr.Blocks:
    """Construct the cockpit ``gr.Blocks`` UI.

    Theme is supplied at ``launch()`` time per Gradio 6.0 API.
    """
    with gr.Blocks(title="HFAO Cockpit") as demo:
        gr.Markdown("# HFAO Observatory")
        with gr.Row():
            project = gr.Dropdown(
                choices=[DEFAULT_PROJECT],
                value=DEFAULT_PROJECT,
                label="Project",
                allow_custom_value=True,
                scale=2,
            )
            refresh_btn = gr.Button("Refresh", scale=1)

        with gr.Tabs():
            with gr.Tab("Home"):
                _build_home_tab(project, refresh_btn)
            with gr.Tab("Traces"):
                _build_traces_tab(project, refresh_btn)
            with gr.Tab("Trace detail") as trace_detail_tab:
                _build_trace_detail_tab(project, trace_detail_tab)
            with gr.Tab("Live tail"):
                _build_live_tail_tab(project)
            with gr.Tab("Datasets"):
                _build_datasets_tab(project, refresh_btn)
            with gr.Tab("Prompts"):
                _build_prompts_tab(project, refresh_btn)
            with gr.Tab("Evals"):
                _build_evals_tab(project, refresh_btn)
            with gr.Tab("Annotations"):
                _build_annotations_tab(project, refresh_btn)
            with gr.Tab("Monitors"):
                _build_monitors_tab(project, refresh_btn)
            with gr.Tab("Costs"):
                _build_costs_tab(project, refresh_btn)
            with gr.Tab("Settings"):
                _build_settings_tab(refresh_btn)
            with gr.Tab("Ask HFAO"):
                _build_ask_hfao_tab(project)

    return demo



def _build_home_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    with gr.Row():
        traces_card = gr.Number(label="Traces (last 200)", value=0, interactive=False)
        spans_card = gr.Number(label="Spans (last 200)", value=0, interactive=False)
        error_rate_card = gr.Number(
            label="Error rate", value=0.0, precision=3, interactive=False
        )
        cost_card = gr.Number(
            label="Total cost USD", value=0.0, precision=4, interactive=False
        )
    recent_html = gr.HTML(label="Recent activity")

    def _home_payload(p: str) -> tuple[int, int, float, float, str]:
        stats = cockpit_quick_stats(p)
        traces = cockpit_recent_traces(p, limit=20)
        return (
            stats["trace_count"],
            stats["span_count"],
            stats["error_rate"],
            stats["total_cost_usd"],
            live_tail.render(traces, max_rows=20),
        )

    project.change(
        _home_payload,
        inputs=[project],
        outputs=[traces_card, spans_card, error_rate_card, cost_card, recent_html],
        api_name="cockpit.read.home",
    )
    refresh_btn.click(
        _home_payload,
        inputs=[project],
        outputs=[traces_card, spans_card, error_rate_card, cost_card, recent_html],
    )


def _build_traces_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    with gr.Row():
        where_box = gr.Textbox(
            label="WHERE filter (optional SQL fragment)",
            value="1=1",
            scale=3,
            placeholder="status = 'error' AND model LIKE 'gpt-%'",
        )
        limit = gr.Slider(
            minimum=10,
            maximum=5000,
            value=200,
            step=10,
            label="Row limit (≤ 5000)",
            scale=2,
        )
    table = gr.Dataframe(
        headers=_TRACES_HEADERS,
        wrap=False,
        interactive=False,
    )

    def _load(p: str, w: str, n: float) -> list[list[Any]]:
        return cockpit_traces_table(p, w or "1=1", limit=int(n))

    project.change(_load, inputs=[project, where_box, limit], outputs=[table])
    where_box.submit(
        _load,
        inputs=[project, where_box, limit],
        outputs=[table],
        api_name="cockpit.read.traces",
    )
    refresh_btn.click(_load, inputs=[project, where_box, limit], outputs=[table])
    limit.release(_load, inputs=[project, where_box, limit], outputs=[table])


def _build_trace_detail_tab(project: gr.Dropdown, _tab: gr.Tab) -> None:
    with gr.Row():
        trace_id_box = gr.Textbox(
            label="Trace ID",
            placeholder="paste a trace_id from the Traces tab",
            scale=4,
        )
        load_btn = gr.Button("Load", variant="primary", scale=1)
    summary = gr.Markdown("_(no trace selected)_")
    with gr.Row():
        with gr.Column(scale=2):
            tree_html = gr.HTML(span_tree.render([]))
        with gr.Column(scale=3):
            chat = gr.Chatbot(label="Trace as chat", height=520)
    with gr.Accordion("Scores", open=False):
        scores_table = gr.Dataframe(
            headers=["name", "value", "string_value", "source", "judge_model"],
            interactive=False,
        )
    with gr.Accordion("Causal edges", open=False):
        edges_table = gr.Dataframe(
            headers=[
                "source",
                "target",
                "edge_type",
                "confidence",
                "method",
                "replay_supported",
            ],
            interactive=False,
        )

    def _load(p: str, tid: str) -> tuple[str, list[trace_chat.ChatTurn], str, list[Any], list[Any]]:
        payload = cockpit_trace_detail(p, tid.strip())
        return (
            payload["tree_html"],
            payload["chat_messages"],
            payload["summary"],
            payload["scores_table"],
            payload["edges_table"],
        )

    load_btn.click(
        _load,
        inputs=[project, trace_id_box],
        outputs=[tree_html, chat, summary, scores_table, edges_table],
        api_name="cockpit.read.trace_detail",
    )
    trace_id_box.submit(
        _load,
        inputs=[project, trace_id_box],
        outputs=[tree_html, chat, summary, scores_table, edges_table],
    )


def _build_live_tail_tab(project: gr.Dropdown) -> None:
    panel = gr.HTML(live_tail.render([]))
    timer = gr.Timer(value=1.0)

    def _tick(p: str) -> str:
        return cockpit_live_tail(p)

    timer.tick(_tick, inputs=[project], outputs=[panel])
    project.change(
        _tick,
        inputs=[project],
        outputs=[panel],
        api_name="cockpit.read.live_tail",
    )


# ---- Week 6 tabs ----------------------------------------------------------


_DATASETS_HEADERS = ["id", "name", "description", "created_at"]


def _build_datasets_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    with gr.Row():
        new_name = gr.Textbox(label="New dataset name", scale=2)
        new_desc = gr.Textbox(label="Description (optional)", scale=3)
        create_btn = gr.Button("Create dataset", variant="primary", scale=1)
    create_msg = gr.Markdown()
    table = gr.Dataframe(headers=_DATASETS_HEADERS, interactive=False)

    def _list(p: str) -> list[list[Any]]:
        return cockpit_datasets_list(p)

    def _create(p: str, name: str, desc: str) -> tuple[str, list[list[Any]]]:
        try:
            ds = cockpit_create_dataset(p, name, desc)
            return f"Created dataset `{ds['id']}` ({ds['name']}).", cockpit_datasets_list(p)
        except (ValueError, KeyError) as exc:
            return f"⚠ {exc}", cockpit_datasets_list(p)

    project.change(_list, inputs=[project], outputs=[table],
                   api_name="cockpit.read.datasets")
    refresh_btn.click(_list, inputs=[project], outputs=[table])
    create_btn.click(
        _create,
        inputs=[project, new_name, new_desc],
        outputs=[create_msg, table],
        api_name="cockpit.write.create_dataset",
    )


_PROMPTS_HEADERS = ["name", "latest_version", "type", "created_by", "created_at"]


def _build_prompts_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    with gr.Row():
        name_box = gr.Textbox(label="Prompt name", scale=2)
        type_dd = gr.Dropdown(choices=["text", "chat"], value="text",
                              label="Type", scale=1)
        label_box = gr.Textbox(label="Set label (optional)", value="production",
                               scale=1)
    content_box = gr.Code(label="Content", language="markdown", lines=4)
    commit_box = gr.Textbox(label="Commit message (optional)")
    with gr.Row():
        create_btn = gr.Button("Create version", variant="primary", scale=1)
    create_msg = gr.Markdown()
    table = gr.Dataframe(headers=_PROMPTS_HEADERS, interactive=False)

    with gr.Accordion("Move a label to a version", open=False):
        with gr.Row():
            move_name = gr.Textbox(label="Prompt name", scale=2)
            move_label = gr.Textbox(label="Label", value="production", scale=1)
            move_version = gr.Number(label="Version", value=1, precision=0, scale=1)
            move_btn = gr.Button("Move label", scale=1)
        move_msg = gr.Markdown()

    def _list(p: str) -> list[list[Any]]:
        return cockpit_prompts_list(p)

    def _create(
        p: str, name: str, typ: str, content: str, label: str, commit: str
    ) -> tuple[str, list[list[Any]]]:
        try:
            v = cockpit_create_prompt(
                p, name, content or "", typ, label or "", commit_message=commit or ""
            )
            return (
                f"Created `{v['name']}` v{v['version']}"
                + (f" → label `{label}`" if label else ""),
                cockpit_prompts_list(p),
            )
        except (ValueError, KeyError) as exc:
            return f"⚠ {exc}", cockpit_prompts_list(p)

    def _move(p: str, name: str, label: str, version: float) -> tuple[str, list[list[Any]]]:
        try:
            prompt = cockpit_set_prompt_label(p, name, label, int(version))
            msg = f"`{prompt['name']}` label `{label}` → v{prompt['version']}"
            return msg, cockpit_prompts_list(p)
        except (ValueError, KeyError) as exc:
            return f"⚠ {exc}", cockpit_prompts_list(p)

    project.change(_list, inputs=[project], outputs=[table],
                   api_name="cockpit.read.prompts")
    refresh_btn.click(_list, inputs=[project], outputs=[table])
    create_btn.click(
        _create,
        inputs=[project, name_box, type_dd, content_box, label_box, commit_box],
        outputs=[create_msg, table],
        api_name="cockpit.write.create_prompt",
    )
    move_btn.click(
        _move,
        inputs=[project, move_name, move_label, move_version],
        outputs=[move_msg, table],
        api_name="cockpit.write.move_prompt_label",
    )


_EVALS_HEADERS = ["eval_run_id", "score_count", "mean_value", "started_at", "finished_at"]


def _build_evals_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown(EVALS_DEFER_NOTICE)
    table = gr.Dataframe(headers=_EVALS_HEADERS, interactive=False)

    def _list(p: str) -> list[list[Any]]:
        return cockpit_evals_list(p)

    project.change(_list, inputs=[project], outputs=[table],
                   api_name="cockpit.read.evals")
    refresh_btn.click(_list, inputs=[project], outputs=[table])


_ANNOTATIONS_HEADERS = ["id", "name", "filter_query", "created_at"]


def _build_annotations_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    with gr.Row():
        name_box = gr.Textbox(label="Queue name", scale=2)
        filter_box = gr.Textbox(
            label="Filter (SQL WHERE)", value="status = 'error'", scale=3
        )
        schema_box = gr.Textbox(
            label="Score schema (comma-separated names)",
            value="quality,helpfulness",
            scale=2,
        )
        create_btn = gr.Button("Create queue", variant="primary", scale=1)
    create_msg = gr.Markdown()
    table = gr.Dataframe(headers=_ANNOTATIONS_HEADERS, interactive=False)

    def _list(p: str) -> list[list[Any]]:
        return cockpit_annotation_queues_list(p)

    def _create(p: str, n: str, f: str, s: str) -> tuple[str, list[list[Any]]]:
        try:
            q = cockpit_create_annotation_queue(p, n, f or "1=1", s or "")
            return f"Created queue `{q['id']}` ({q['name']}).", cockpit_annotation_queues_list(p)
        except (ValueError, KeyError) as exc:
            return f"⚠ {exc}", cockpit_annotation_queues_list(p)

    project.change(_list, inputs=[project], outputs=[table],
                   api_name="cockpit.read.annotation_queues")
    refresh_btn.click(_list, inputs=[project], outputs=[table])
    create_btn.click(
        _create,
        inputs=[project, name_box, filter_box, schema_box],
        outputs=[create_msg, table],
        api_name="cockpit.write.create_annotation_queue",
    )


_MONITORS_HEADERS = ["id", "name", "window", "threshold", "enabled"]


def _build_monitors_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown(MONITORS_DEFER_NOTICE)
    table = gr.Dataframe(headers=_MONITORS_HEADERS, interactive=False)
    with gr.Row():
        nl_box = gr.Textbox(
            label="Natural-language description",
            value="error rate over the last hour",
            scale=3,
        )
        window_box = gr.Textbox(label="Window", value="1 hour", scale=1)
        preview_btn = gr.Button("Preview SQL", scale=1)
    preview = gr.Code(label="SQL preview (STUB)", language="sql")

    def _list(p: str) -> list[list[Any]]:
        return cockpit_monitors_list(p)

    def _preview(nl: str, window: str) -> str:
        return cockpit_monitor_nl_preview(nl, window)

    project.change(_list, inputs=[project], outputs=[table],
                   api_name="cockpit.read.monitors")
    refresh_btn.click(_list, inputs=[project], outputs=[table])
    preview_btn.click(
        _preview,
        inputs=[nl_box, window_box],
        outputs=[preview],
        api_name="cockpit.read.monitor_preview",
    )


_COSTS_HEADERS_TAIL = ["total_cost_usd", "total_tokens", "call_count"]


def _build_costs_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    with gr.Row():
        group_dd = gr.Dropdown(
            choices=["model", "user_id", "agent_id", "prompt_name", "date"],
            value="model",
            label="Group by",
            scale=2,
        )
        window_box = gr.Textbox(label="Window", value="7d", scale=1)
    table = gr.Dataframe(interactive=False)

    def _rollup(p: str, group: str, window: str) -> list[list[Any]]:
        try:
            return cockpit_cost_rollup(p, [group], window)
        except (ValueError, KeyError):
            return []

    project.change(
        _rollup,
        inputs=[project, group_dd, window_box],
        outputs=[table],
        api_name="cockpit.read.costs",
    )
    refresh_btn.click(_rollup, inputs=[project, group_dd, window_box], outputs=[table])
    group_dd.change(_rollup, inputs=[project, group_dd, window_box], outputs=[table])
    window_box.submit(_rollup, inputs=[project, group_dd, window_box], outputs=[table])


def _build_settings_tab(refresh_btn: gr.Button) -> None:
    settings_md = gr.Markdown()
    keys_table = gr.Dataframe(
        headers=["id", "prefix", "role", "name", "created_at", "revoked"],
        interactive=False,
    )

    def _render() -> tuple[str, list[list[Any]]]:
        payload = cockpit_settings_payload()
        lines = [
            "### Effective configuration",
            f"- **Project**: `{payload['project']}`",
            f"- **Environment**: `{payload['environment']}`",
            f"- **Backend**: `{payload['backend']}`",
            f"- **DuckDB path**: `{payload['duckdb_path']}`",
            f"- **Control plane**: `{payload['control_plane_dsn']}`",
            f"- **Redaction profile**: `{payload['redaction_profile']}`",
            f"- **Judge**: `{payload['judge_provider']}` / `{payload['judge_model']}`",
            f"- **MCP read-only**: `{payload['mcp_read_only']}`",
            "",
            "### API keys",
        ]
        rows = [
            [k["id"], k["prefix"], k["role"], k["name"], k["created_at"],
             "yes" if k["revoked"] else "no"]
            for k in payload["api_keys"]
        ]
        if not rows:
            lines.append("_No API keys issued yet. Use `hfao keys issue` (Week 6 §13)._")
        return "\n".join(lines), rows

    refresh_btn.click(_render, outputs=[settings_md, keys_table])
    # Render once at build time so the tab isn't empty on first paint.
    initial_md, initial_keys = _render()
    settings_md.value = initial_md
    keys_table.value = initial_keys


def _build_ask_hfao_tab(project: gr.Dropdown) -> None:
    gr.Markdown(
        "Ask grounded questions about your traces. v1 routes the question "
        "through the §9 MCP read tools deterministically — try `recent traces`, "
        "`failures`, `cost`, or `trace <id>`."
    )
    chatbot = gr.Chatbot(height=420)
    msg = gr.Textbox(label="Ask HFAO", placeholder="failures · cost · trace <id>")

    def _respond(
        message: str, history: list[tuple[str, str]], p: str
    ) -> tuple[str, list[tuple[str, str]]]:
        answer = cockpit_ask_hfao(p, message or "")
        return "", [*(history or []), (message or "", answer)]

    msg.submit(
        _respond,
        inputs=[msg, chatbot, project],
        outputs=[msg, chatbot],
        api_name="cockpit.read.ask_hfao",
    )


demo = build_blocks()


if __name__ == "__main__":
    theme = gr.themes.Soft(primary_hue="indigo", secondary_hue="violet")
    demo.launch(mcp_server=True, server_port=7860, theme=theme)
