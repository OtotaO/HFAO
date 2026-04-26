"""HFAO CLI (``hfao`` on PATH).

SPEC §15.2 week 5 schedules `hfao up` / `hfao migrate` / `hfao seed`;
this module ships the portfolio-visible demo surface ahead of that work:

  hfao dashboard   — read-only Rich render of storage + ingest health
  hfao ingest send — write a synthetic OTel GenAI span to the configured
                     backend (in-process); prints the trace_id
  hfao query N     — tabulate the most recent N traces from events_current
  hfao --version   — print the installed package version

The demo uses the single-file HF Space shape (DuckDB embedded). The in-
process ingest path reuses the Week 3 normalize → redact → body-offload
pipeline, so `hfao ingest send && hfao query 1` produces a real, PII-
safe row in the same terminal session without spinning up Granian.
"""

from __future__ import annotations

import random
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, cast

import msgspec
import typer
from rich.box import ROUNDED
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hfao.config import HFAOConfig
from hfao.ingest.body_offload import BodyOffloader, LocalBodyStore
from hfao.ingest.normalize import normalize, normalize_scores
from hfao.ingest.redact import RedactionConfig, Redactor
from hfao.schema.events import CostBreakdown, Observation
from hfao.schema.otlp import Span
from hfao.storage.duckdb_backend import DuckDBBackend

app = typer.Typer(
    name="hfao",
    help="Hugging Face Agent Observatory — demo CLI.",
    no_args_is_help=True,
    add_completion=False,
)

ingest_app = typer.Typer(
    help="Ingest plane demo commands (SPEC §5 / §7).",
    no_args_is_help=True,
)
app.add_typer(ingest_app, name="ingest")


# ---- shared helpers ----


def _version_string() -> str:
    try:
        return version("hfao")
    except PackageNotFoundError:
        return "0.0.0-dev"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"hfao {_version_string()}")
        raise typer.Exit()


@app.callback()
def root_callback(
    _version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the installed hfao version and exit.",
        ),
    ] = False,
) -> None:
    """HFAO CLI root — hands off to subcommands (dashboard / ingest / query)."""


def _duckdb_path_for(cfg: HFAOConfig) -> str:
    """Resolve the DuckDB path, falling back to a cwd-relative default.

    The SPEC Appendix A default of ``/data/hfao.duckdb`` requires root on
    most systems; for the demo CLI we prefer ``./hfao.duckdb`` unless the
    user has set ``HFAO_DUCKDB_PATH`` explicitly.
    """
    raw = cfg.duckdb_path
    if raw == "/data/hfao.duckdb":
        return str(Path.cwd() / "hfao.duckdb")
    return raw


@contextmanager
def _open_backend(cfg: HFAOConfig) -> Iterator[DuckDBBackend]:
    path = _duckdb_path_for(cfg)
    backend = DuckDBBackend(path)
    backend.init_schema()
    try:
        yield backend
    finally:
        backend.close()


# ---- synthetic span generator (used by `ingest send` and `screenshot`) ----

_MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gpt-4o-mini",
    "gpt-4o",
    "qwen3-8b",
]
_OPS = ["chat", "invoke_agent", "execute_tool", "embeddings"]
_USERS = ["alice@example.com", "bob+dev@hfao.dev", "+14155551212 carol"]


def _synthetic_span(
    *,
    project_id: str,
    model: str | None = None,
    operation: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    status_fail: bool | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> Span:
    rng = rng or random.Random()
    now = now or datetime.now(timezone.utc)
    mdl = model or rng.choice(_MODELS)
    op = operation or rng.choice(_OPS)
    lat = latency_ms if latency_ms is not None else rng.randint(120, 3200)
    _ = cost_usd  # cost is derived from usage on the way out via CostBreakdown
    failing = status_fail if status_fail is not None else (rng.random() < 0.18)

    prompt_tokens = rng.randint(30, 800)
    completion_tokens = rng.randint(20, 400)
    total_tokens = prompt_tokens + completion_tokens

    trace_id_hex = secrets.token_hex(16)
    span_id_hex = secrets.token_hex(8)
    user = rng.choice(_USERS)

    end = now
    start = end - timedelta(milliseconds=lat)

    attributes: dict[str, object] = {
        "gen_ai.operation.name": op,
        "gen_ai.request.model": mdl,
        "gen_ai.response.model": mdl,
        "gen_ai.usage.input_tokens": prompt_tokens,
        "gen_ai.usage.output_tokens": completion_tokens,
        "gen_ai.usage.total_tokens": total_tokens,
        "gen_ai.request.temperature": round(rng.uniform(0.0, 1.0), 2),
        "gen_ai.conversation.id": f"conv-{uuid.uuid4().hex[:8]}",
        "user.id": user,  # redactor will catch the PII
        "tag.tags": ["demo", op],
        "hfao.project_id": project_id,
        "gen_ai.input.messages": (
            f'[{{"role":"user","content":"contact: {user}"}}]'
        ),
        "gen_ai.output.messages": '[{"role":"assistant","content":"ok"}]',
    }

    return Span(
        trace_id=trace_id_hex,
        span_id=span_id_hex,
        name=op,
        start_time=start,
        end_time=end,
        status="error" if failing else "ok",
        status_message="tool timeout" if failing else None,
        attributes=attributes,
    )


def _ingest_one(
    span: Span,
    *,
    backend: DuckDBBackend,
    redactor: Redactor,
    offloader: BodyOffloader,
    project_id: str,
    cost_usd: float | None = None,
) -> Observation:
    observations = normalize(span, default_project_id=project_id)
    scores = normalize_scores(span)
    to_write: list[Observation] = []
    for obs in observations:
        processed = offloader.offload(redactor.redact_observation(obs))
        if cost_usd is not None:
            # The normalizer leaves CostBreakdown empty — OTel GenAI has no
            # standard cost attribute yet. Demo cost shows the rollup path
            # works end-to-end and keeps the dashboard column meaningful.
            processed = msgspec.structs.replace(
                processed,
                cost=CostBreakdown(
                    input_cost_usd=round(cost_usd * 0.25, 6),
                    output_cost_usd=round(cost_usd * 0.75, 6),
                    total_cost_usd=round(cost_usd, 6),
                ),
            )
        to_write.append(processed)
    backend.write_events(to_write)
    if scores:
        backend.write_scores(scores)
    return to_write[0]


# ---- commands ----


@ingest_app.command("send")
def ingest_send(
    count: Annotated[
        int, typer.Option("--count", "-n", min=1, help="Number of spans to send.")
    ] = 1,
    vary: Annotated[
        bool,
        typer.Option(
            "--vary/--no-vary",
            help="Randomize model / latency / cost / status across spans.",
        ),
    ] = False,
    model: Annotated[str | None, typer.Option(help="Override model name.")] = None,
    seed: Annotated[
        int | None,
        typer.Option(help="Seed the RNG for reproducible synthetic spans."),
    ] = None,
) -> None:
    """Send one or more synthetic OTel GenAI spans into the configured backend."""
    cfg = HFAOConfig.from_env()
    console = Console()
    rng = random.Random(seed) if seed is not None else random.Random()

    redactor = Redactor(RedactionConfig())
    store_root = Path(cfg.bodies_path)
    if str(store_root) == "/data/bodies":
        store_root = Path.cwd() / "hfao-bodies"
    offloader = BodyOffloader(
        LocalBodyStore(store_root),
        threshold_bytes=cfg.body_offload_threshold_bytes,
    )

    sent: list[Observation] = []
    with _open_backend(cfg) as backend:
        for _ in range(count):
            span = _synthetic_span(
                project_id=cfg.project,
                model=model,
                rng=rng if vary else random.Random(seed if seed is not None else 42),
            )
            obs = _ingest_one(
                span,
                backend=backend,
                redactor=redactor,
                offloader=offloader,
                project_id=cfg.project,
                cost_usd=round(rng.uniform(0.0005, 0.12), 6),
            )
            sent.append(obs)

    for obs in sent:
        console.print(f"[green]ingested[/green] trace_id={obs.trace_id}")


@app.command()
def up(
    host: Annotated[
        str, typer.Option("--host", help="Bind host for the cockpit.")
    ] = "0.0.0.0",
    port: Annotated[
        int, typer.Option("--port", "-p", min=1, max=65535, help="Bind port.")
    ] = 7860,
    mcp_server: Annotated[
        bool,
        typer.Option(
            "--mcp-server/--no-mcp-server",
            help="Mount Gradio's auto-MCP export (cockpit.read.* / cockpit.write.*).",
        ),
    ] = True,
    share: Annotated[
        bool,
        typer.Option("--share/--no-share", help="Tunnel via gradio.live for remote demos."),
    ] = False,
) -> None:
    """Boot the HFAO Cockpit (apps/cockpit/cockpit.py) — SPEC §10.1."""
    # Imported lazily so `hfao --version` / `hfao migrate` don't pay
    # the Gradio import cost on cold start.
    import gradio as gr  # noqa: PLC0415 — lazy boot path

    from apps.cockpit.cockpit import demo

    # Gradio doesn't re-export `themes` at top-level; pyright flags the
    # access. Use the canonical attribute path with a strict-mode shim.
    themes_mod = gr.themes  # type: ignore[reportPrivateImportUsage]
    theme = themes_mod.Soft(primary_hue="indigo", secondary_hue="violet")
    demo.launch(
        server_name=host,
        server_port=port,
        mcp_server=mcp_server,
        share=share,
        theme=theme,
    )


@app.command()
def migrate() -> None:
    """Initialize storage schemas for the configured backend + control plane.

    SPEC §15.2 Week 5. Idempotent: ``init_schema()`` is a CREATE TABLE
    IF NOT EXISTS sequence on every backend, so re-running is safe.
    """
    cfg = HFAOConfig.from_env()
    console = Console()
    with _open_backend(cfg) as backend:
        backend.init_schema()
        console.print(
            f"[green]✓[/green] hot-tier schema initialised — backend={cfg.backend} "
            f"path={_duckdb_path_for(cfg)}"
        )

    if cfg.backend == "clickhouse" and cfg.clickhouse_dsn:
        from hfao.storage.clickhouse_backend import ClickHouseBackend

        ch = ClickHouseBackend(cfg.clickhouse_dsn)
        try:
            ch.init_schema()
            console.print(
                f"[green]✓[/green] ClickHouse schema initialised — dsn={cfg.clickhouse_dsn}"
            )
        finally:
            ch.close()

    from hfao.storage.control_plane import ControlPlane

    dsn = cfg.control_plane_dsn
    if dsn == "sqlite:///data/control.db":
        # Default Appendix A path needs root; fall back like DuckDB.
        dsn = f"sqlite:///{Path.cwd() / 'hfao-control.db'}"
    cp = ControlPlane(dsn)
    cp.init_schema()
    cp.close()
    console.print(f"[green]✓[/green] control plane schema initialised — dsn={dsn}")


@app.command()
def seed(
    count: Annotated[
        int, typer.Option("--count", "-n", min=1, help="Number of synthetic traces.")
    ] = 25,
    seed: Annotated[
        int, typer.Option(help="RNG seed for reproducible cockpit demos.")
    ] = 1337,
) -> None:
    """Seed the configured backend with synthetic traces for the cockpit demo.

    Uses the same in-process ingest pipeline as ``hfao ingest send``: each
    span runs through the §5.6 normalizer + §6.5 redactor + §6.6 body
    offloader before landing in storage. ``hfao up`` after this gives the
    Home / Traces / Live tail tabs something to render.
    """
    cfg = HFAOConfig.from_env()
    console = Console()
    rng = random.Random(seed)

    redactor = Redactor(RedactionConfig())
    store_root = Path(cfg.bodies_path)
    if str(store_root) == "/data/bodies":
        store_root = Path.cwd() / "hfao-bodies"
    offloader = BodyOffloader(
        LocalBodyStore(store_root),
        threshold_bytes=cfg.body_offload_threshold_bytes,
    )

    with _open_backend(cfg) as backend:
        backend.init_schema()
        for _ in range(count):
            span = _synthetic_span(project_id=cfg.project, rng=rng)
            _ingest_one(
                span,
                backend=backend,
                redactor=redactor,
                offloader=offloader,
                project_id=cfg.project,
                cost_usd=round(rng.uniform(0.0005, 0.12), 6),
            )

    console.print(
        f"[green]✓[/green] seeded {count} traces into project={cfg.project} "
        f"(seed={seed})"
    )


@app.command()
def query(
    n: Annotated[int, typer.Argument(min=1, help="How many traces to show.")] = 20,
) -> None:
    """Tabulate the most recent N traces from ``events_current``."""
    cfg = HFAOConfig.from_env()
    console = Console()
    with _open_backend(cfg) as backend:
        rows = backend.list_traces(cfg.project, limit=n)
        op_by_trace = {
            r["trace_id"]: _lookup_name_for(backend, cfg.project, r["trace_id"])
            for r in rows
        }

    table = Table(title=f"hfao query — last {n} traces ({cfg.project})", box=ROUNDED)
    table.add_column("trace_id", style="cyan", no_wrap=True)
    table.add_column("op", style="magenta")
    table.add_column("spans", justify="right")
    table.add_column("latency", justify="right", style="yellow")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right", style="green")
    table.add_column("status", justify="center")
    table.add_column("session", style="blue", no_wrap=True)

    for r in rows:
        latency_ms = _latency_ms(r)
        status = "[red]error[/red]" if r.get("has_error") else "[green]ok[/green]"
        table.add_row(
            str(r["trace_id"])[:16] + "…",
            op_by_trace.get(r["trace_id"], "—"),
            str(r["span_count"]),
            f"{latency_ms} ms",
            str(r.get("total_tokens") or 0),
            f"${float(r.get('total_cost_usd') or 0):.4f}",
            status,
            (r.get("session_id") or "—")[:18],
        )

    console.print(table)


@app.command()
def dashboard() -> None:
    """Render a one-shot dashboard of storage + ingest health."""
    cfg = HFAOConfig.from_env()
    console = Console()
    render_dashboard(console=console, cfg=cfg)


# ---- shared render helpers (also used by scripts/generate_cli_screenshot.py) ----


def render_dashboard(*, console: Console, cfg: HFAOConfig) -> None:
    with _open_backend(cfg) as backend:
        stats = _collect_stats(backend, cfg)
        recent = backend.list_traces(cfg.project, limit=10)
        op_by_trace = {
            r["trace_id"]: _lookup_name_for(backend, cfg.project, r["trace_id"])
            for r in recent
        }
        redaction_rate = _compute_redaction_rate(backend, cfg)

    header = Text()
    header.append("HFAO Observatory", style="bold cyan")
    header.append("   ·   ", style="dim")
    header.append(
        f"project={cfg.project}   backend={cfg.backend}   path={_duckdb_path_for(cfg)}",
        style="dim",
    )
    console.print(Panel(header, box=ROUNDED, border_style="cyan"))

    console.print(
        Columns(
            [
                _panel_kv(
                    "Storage",
                    [
                        ("rows (events)", stats["rows_events"]),
                        ("rows (scores)", stats["rows_scores"]),
                        ("rows (causal_edges)", stats["rows_edges"]),
                        ("distinct traces", stats["distinct_traces"]),
                    ],
                    border="blue",
                ),
                _panel_kv(
                    "Ingest (last 5 min)",
                    [
                        ("spans / minute", stats["spans_per_min"]),
                        ("tokens / minute", stats["tokens_per_min"]),
                        ("error rate", f"{stats['error_rate']:.1%}"),
                        ("redaction hits", f"{redaction_rate:.1%}"),
                    ],
                    border="magenta",
                ),
                _panel_kv(
                    "Models in play",
                    cast("list[tuple[str, object]]", stats["top_models"])
                    or [("—", 0)],
                    border="yellow",
                ),
            ],
            expand=True,
            equal=True,
        )
    )

    table = Table(title="Recent traces", box=ROUNDED, expand=True)
    table.add_column("trace_id", style="cyan", no_wrap=True)
    table.add_column("op", style="magenta")
    table.add_column("spans", justify="right")
    table.add_column("latency", justify="right", style="yellow")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right", style="green")
    table.add_column("status", justify="center")

    for r in recent:
        status = "[red]error[/red]" if r.get("has_error") else "[green]ok[/green]"
        table.add_row(
            str(r["trace_id"])[:16] + "…",
            op_by_trace.get(r["trace_id"], "—"),
            str(r["span_count"]),
            f"{_latency_ms(r)} ms",
            str(r.get("total_tokens") or 0),
            f"${float(r.get('total_cost_usd') or 0):.4f}",
            status,
        )

    console.print(table)

    footer = Text()
    footer.append("hfao ingest send", style="bold")
    footer.append(" · ", style="dim")
    footer.append("hfao query 20", style="bold")
    footer.append(" · ", style="dim")
    footer.append("hfao dashboard", style="bold")
    footer.append("     ", style="dim")
    footer.append(f"v{_version_string()}", style="dim cyan")
    console.print(footer, justify="center")


def _panel_kv(title: str, rows: list[tuple[str, object]], *, border: str) -> Panel:
    body = Table.grid(padding=(0, 2))
    body.add_column(justify="left", style="dim")
    body.add_column(justify="right", style="bold")
    for k, v in rows:
        body.add_row(str(k), str(v))
    return Panel(body, title=title, box=ROUNDED, border_style=border)


def _latency_ms(row: dict[str, object]) -> int:
    first = row.get("first_start")
    last = row.get("last_end")
    if isinstance(first, datetime) and isinstance(last, datetime):
        return max(int((last - first).total_seconds() * 1000), 0)
    return 0


def _lookup_name_for(backend: DuckDBBackend, project_id: str, trace_id: str) -> str:
    obs = backend.get_trace(project_id, trace_id)
    if not obs:
        return "—"
    return obs[0].name


def _collect_stats(backend: DuckDBBackend, cfg: HFAOConfig) -> dict[str, object]:
    def _scalar(sql: str) -> int:
        rows = backend.execute_readonly_sql(cfg.project, sql)
        if not rows:
            return 0
        first = next(iter(rows[0].values()))
        try:
            return int(first)
        except (TypeError, ValueError):
            return 0

    rows_events = _scalar("SELECT count() AS n FROM events_current")
    rows_scores = _scalar("SELECT count() AS n FROM scores")
    rows_edges = _scalar("SELECT count() AS n FROM causal_edges")
    distinct_traces = _scalar("SELECT count(DISTINCT trace_id) AS n FROM events_current")

    window_rows = backend.execute_readonly_sql(
        cfg.project,
        """
        SELECT count() AS spans,
               sum(total_tokens) AS tokens,
               avg(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS err_rate
        FROM events_current
        WHERE start_time >= now() - INTERVAL 5 MINUTE
        """,
    )
    window = window_rows[0] if window_rows else {}
    spans = int(window.get("spans") or 0)
    tokens = int(window.get("tokens") or 0)
    err_rate = float(window.get("err_rate") or 0.0)

    top_models_rows = backend.execute_readonly_sql(
        cfg.project,
        """
        SELECT coalesce(nullif(model, ''), '(none)') AS model, count() AS c
        FROM events_current
        WHERE start_time >= now() - INTERVAL 1 HOUR
        GROUP BY 1
        ORDER BY c DESC
        LIMIT 5
        """,
    )
    top_models = [(str(r["model"]), int(r["c"])) for r in top_models_rows]

    return {
        "rows_events": rows_events,
        "rows_scores": rows_scores,
        "rows_edges": rows_edges,
        "distinct_traces": distinct_traces,
        "spans_per_min": round(spans / 5.0, 1),
        "tokens_per_min": round(tokens / 5.0, 1),
        "error_rate": err_rate,
        "top_models": top_models,
    }


def _compute_redaction_rate(backend: DuckDBBackend, cfg: HFAOConfig) -> float:
    rows = backend.execute_readonly_sql(
        cfg.project,
        """
        SELECT
          count() AS total,
          sum(CASE WHEN input LIKE '%[REDACTED:%' OR output LIKE '%[REDACTED:%'
                   THEN 1 ELSE 0 END) AS hits
        FROM events_current
        """,
    )
    if not rows:
        return 0.0
    total = int(rows[0].get("total") or 0)
    hits = int(rows[0].get("hits") or 0)
    if total == 0:
        return 0.0
    return hits / total


__all__ = ["app", "render_dashboard"]
