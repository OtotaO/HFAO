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

eval_app = typer.Typer(
    help="Eval engine (SPEC §8.2): offline runs + CI gates.",
    no_args_is_help=True,
)
app.add_typer(eval_app, name="eval")

parquet_app = typer.Typer(
    help="Warm-tier Parquet export (SPEC §16 Q-13).",
    no_args_is_help=True,
)
app.add_typer(parquet_app, name="parquet")

retention_app = typer.Typer(
    help="Retention worker (SPEC §6.4): hot-tier + body purge.",
    no_args_is_help=True,
)
app.add_typer(retention_app, name="retention")


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


@eval_app.command("run")
def eval_run(
    dataset: Annotated[str, typer.Argument(help="Dataset id OR name.")],
    evaluators: Annotated[
        str,
        typer.Option(
            "--evaluators",
            "-e",
            help="Comma-separated evaluator names.",
        ),
    ] = "exact_match",
    project: Annotated[
        str | None,
        typer.Option(help="Project id; defaults to HFAO_PROJECT."),
    ] = None,
    runtime_url: Annotated[
        str | None,
        typer.Option("--runtime", help="HTTP runtime URL; POST {input, metadata}."),
    ] = None,
    gate: Annotated[
        str | None,
        typer.Option(
            help='CI gate expression, e.g. "exact_match>=0.9". '
            "Exits 1 on failure."
        ),
    ] = None,
) -> None:
    """Run evaluators offline against a dataset and print a summary table."""
    from rich.console import Console
    from rich.table import Table

    from hfao.compute.eval.runner import run_eval as _run_eval
    from hfao.config import HFAOConfig

    cfg = HFAOConfig.from_env()
    project_id = project or cfg.project
    evaluator_names = [e.strip() for e in evaluators.split(",") if e.strip()]
    if not evaluator_names:
        raise typer.BadParameter("at least one evaluator required")
    result = _run_eval(
        project=project_id,
        dataset=dataset,
        evaluators=evaluator_names,
        runtime_url=runtime_url,
        gate_expression=gate,
    )
    console = Console()
    table = Table(title=f"Eval run {result['id']}")
    table.add_column("metric")
    table.add_column("mean", justify="right")
    for name, value in result["summary"].items():
        table.add_row(name, f"{value:.4f}")
    console.print(table)
    console.print(
        f"items={result['sample_count']} status={result['status']} "
        f"gate={result['gate_expression'] or 'none'} passed={result['gate_passed']}"
    )
    if gate and result["gate_passed"] is False:
        raise typer.Exit(code=1)


@parquet_app.command("export")
def parquet_export(
    out: Annotated[
        Path,
        typer.Argument(
            help="Local directory or file path to write Parquet shards into.",
        ),
    ],
    project: Annotated[
        str | None,
        typer.Option(help="Project id; defaults to HFAO_PROJECT."),
    ] = None,
    date_from: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="ISO date/datetime lower bound (inclusive). Default: 7 days ago.",
        ),
    ] = None,
    date_to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="ISO date/datetime upper bound (exclusive). Default: now.",
        ),
    ] = None,
    hf_bucket: Annotated[
        str | None,
        typer.Option(
            "--hf-bucket",
            help="HF Bucket URL prefix (e.g. f8n-ai/hfao-warm). Uploads after export.",
        ),
    ] = None,
) -> None:
    """One-shot Parquet export per §16 Q-13.

    Reads the closed-hour partitions of events for ``project`` over
    [from, to) and materialises them as Parquet under
    ``{out}/project_id={project}/year={YYYY}/month={MM}/day={DD}/hour={HH}/part-0.parquet``
    matching the §4.4 partition convention. When ``--hf-bucket`` is set, the
    same paths are mirrored to ``hf://buckets/{bucket}/hfao/v1/events/...``.

    The continuous auto-sync worker (``storage/parquet_sync.py``) is
    deferred to v1.1 per §16 Q-13. This command is the v1 warm-tier path.
    """
    from datetime import datetime, timezone

    from hfao.config import HFAOConfig

    cfg = HFAOConfig.from_env()
    project_id = project or cfg.project

    now_utc = datetime.now(tz=timezone.utc)
    end = datetime.fromisoformat(date_to) if date_to else now_utc
    start = (
        datetime.fromisoformat(date_from)
        if date_from
        else end - timedelta(days=7)
    )
    if end <= start:
        raise typer.BadParameter("--to must be after --from")

    out.mkdir(parents=True, exist_ok=True)

    with _open_backend(cfg) as backend:
        shards = _export_duckdb_parquet(backend, project_id, start, end, out)

    console = Console()
    console.print(
        f"[green]Exported {len(shards)} shard(s)[/green] from "
        f"{start.isoformat()} to {end.isoformat()} into {out}"
    )

    if hf_bucket:
        _upload_shards_to_hf(out, hf_bucket, project_id, shards)
        console.print(
            f"[green]Uploaded shards to hf://{hf_bucket}/hfao/v1/events/[/green]"
        )


def _export_duckdb_parquet(
    backend: DuckDBBackend,
    project_id: str,
    start: datetime,
    end: datetime,
    out: Path,
) -> list[Path]:
    """Materialise per-hour Parquet shards. Returns the written paths."""
    shards: list[Path] = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        hour_end = cur + timedelta(hours=1)
        partition = (
            out
            / f"project_id={project_id}"
            / f"year={cur.year:04d}"
            / f"month={cur.month:02d}"
            / f"day={cur.day:02d}"
            / f"hour={cur.hour:02d}"
        )
        partition.mkdir(parents=True, exist_ok=True)
        shard = partition / "part-0.parquet"
        rows = backend.export_events_to_parquet(
            project_id, start=cur, end=hour_end, out_path=shard.as_posix()
        )
        if rows > 0:
            shards.append(shard)
        elif shard.exists():
            shard.unlink()
        cur = hour_end
    return shards


def _upload_shards_to_hf(
    out: Path, hf_bucket: str, project_id: str, shards: list[Path]
) -> None:
    """Mirror local shards to an HF Bucket under hfao/v1/events/.

    ``project_id`` is already embedded in the local partition path
    (``out/project_id={project_id}/...``); the relative paths preserve it
    when uploaded, so the parameter is captured here only to make the
    contract explicit at the call site.
    """
    del project_id
    from huggingface_hub import HfApi

    api = HfApi()
    for shard in shards:
        rel = shard.relative_to(out)
        remote = f"hfao/v1/events/{rel.as_posix()}"
        api.upload_file(
            path_or_fileobj=str(shard),
            path_in_repo=remote,
            repo_id=hf_bucket,
            repo_type="dataset",
        )


@retention_app.command("set")
def retention_set(
    project: Annotated[str, typer.Argument(help="Project id.")],
    hot_days: Annotated[int, typer.Option(help="Hot-tier retention.")] = 30,
    warm_days: Annotated[int, typer.Option(help="Warm-tier retention.")] = 365,
    bodies_days: Annotated[int, typer.Option(help="Offloaded-body retention.")] = 90,
    enabled: Annotated[bool, typer.Option(help="Enable retention for this project.")] = True,
) -> None:
    """Create or update a project's retention policy (§6.4)."""
    from hfao.config import HFAOConfig
    from hfao.storage.control_plane import ControlPlane

    cfg = HFAOConfig.from_env()
    cp = ControlPlane(cfg.control_plane_dsn)
    cp.init_schema()
    try:
        policy = cp.upsert_retention_policy(
            project_id=project,
            hot_days=hot_days,
            warm_days=warm_days,
            bodies_days=bodies_days,
            enabled=enabled,
        )
    finally:
        cp.close()
    Console().print(
        f"[green]Saved retention policy[/green] for project {project}: {policy}"
    )


@retention_app.command("run")
def retention_run(
    project: Annotated[
        str | None,
        typer.Option(help="Project id; default: run for every project with a policy."),
    ] = None,
) -> None:
    """Run one retention pass against the configured projects (§6.4)."""
    from hfao.compute.retention import run_once
    from hfao.config import HFAOConfig
    from hfao.storage.control_plane import ControlPlane

    cfg = HFAOConfig.from_env()
    bodies_root = Path(cfg.bodies_path) if cfg.bodies_path else None
    with _open_backend(cfg) as backend:
        cp = ControlPlane(cfg.control_plane_dsn)
        cp.init_schema()
        try:
            del project  # the worker uses all enabled policies in one pass
            result = run_once(backend, cp, bodies_root=bodies_root)
        finally:
            cp.close()
    console = Console()
    console.print(
        f"[green]Retention pass completed in "
        f"{(result.finished_at - result.started_at).total_seconds():.2f}s[/green]"
    )
    for pid, counts in result.per_project.items():
        console.print(
            f"  {pid}: events={counts['events']} "
            f"scores={counts['scores']} causal_edges={counts['causal_edges']}"
        )
    console.print(f"  body files pruned: {result.bodies_pruned}")
    if result.errors:
        for err in result.errors:
            console.print(f"  [yellow]error:[/yellow] {err}")


@retention_app.command("show")
def retention_show(
    project: Annotated[
        str | None,
        typer.Option(help="Project id; default: list all policies."),
    ] = None,
) -> None:
    """Print the retention policy for a project (or all)."""
    from hfao.config import HFAOConfig
    from hfao.storage.control_plane import ControlPlane

    cfg = HFAOConfig.from_env()
    cp = ControlPlane(cfg.control_plane_dsn)
    cp.init_schema()
    console = Console()
    try:
        if project:
            console.print(cp.get_retention_policy(project_id=project))
        else:
            for p in cp.list_retention_policies():
                console.print(p)
    finally:
        cp.close()


__all__ = ["app", "render_dashboard"]
