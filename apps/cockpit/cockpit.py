"""HFAO Cockpit — Gradio 6 single-file UI.

SPEC §10. Mounts the four Week 5 tabs (Home, Traces, Trace detail,
Live tail) on a single ``gr.Blocks``; the remaining eight §10.2 tabs
land in Week 6. Imports from ``hfao.*`` only — no SQL leaks outside
``packages/hfao/storage`` (Appendix C rule 4).

Launch:

    python -m hfao.cli up
    # → apps.cockpit.cockpit:demo.launch(mcp_server=True, server_port=7860)

Every read-side handler is registered with ``api_name="cockpit.read.*"``
so it auto-exports as a Gradio MCP tool when ``mcp_server=True``
(§10.4). Write-side handlers (Week 6+) will use ``cockpit.write.*``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from typing import Any, cast

import gradio as gr
import msgspec
from hfao.config import HFAOConfig
from hfao.schema.events import Observation
from hfao.storage import StorageBackend
from hfao.storage.clickhouse_backend import ClickHouseBackend
from hfao.storage.duckdb_backend import DuckDBBackend

from apps.cockpit.components import live_tail, span_tree, trace_chat

CONFIG = HFAOConfig.from_env()
DEFAULT_PROJECT = CONFIG.project


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

                from apps.cockpit.cockpit_week6 import _build_datasets_tab

                _build_datasets_tab(project, refresh_btn)

            with gr.Tab("Prompts"):

                from apps.cockpit.cockpit_week6 import _build_prompts_tab

                _build_prompts_tab(project, refresh_btn)

            with gr.Tab("Evals"):

                from apps.cockpit.cockpit_week6 import _build_evals_tab

                _build_evals_tab(project, refresh_btn)

            with gr.Tab("Annotations"):

                from apps.cockpit.cockpit_week6 import _build_annotations_tab

                _build_annotations_tab(project, refresh_btn)

            with gr.Tab("Monitors"):

                from apps.cockpit.cockpit_week6 import _build_monitors_tab

                _build_monitors_tab(project, refresh_btn)

            with gr.Tab("Costs"):

                from apps.cockpit.cockpit_week6 import _build_costs_tab

                _build_costs_tab(project, refresh_btn)

            with gr.Tab("Settings"):

                from apps.cockpit.cockpit_week6 import _build_settings_tab

                _build_settings_tab(project)

            with gr.Tab("Ask HFAO"):

                from apps.cockpit.cockpit_week6 import _build_ask_hfao_tab

                _build_ask_hfao_tab()



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


demo = build_blocks()


if __name__ == "__main__":
    theme = gr.themes.Soft(primary_hue="indigo", secondary_hue="violet")
    demo.launch(mcp_server=True, server_port=7860, theme=theme)
