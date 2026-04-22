"""Generate ``docs/hfao-cli.png`` — the portfolio CLI screenshot.

Pipeline:

1. Seed an isolated DuckDB with five varied synthetic spans so the
   ``hfao dashboard`` render has real signal (costs, latencies, PII
   redaction hits, mixed ok / error status).
2. Record the render via Rich's ``Console(record=True)`` and export
   self-contained HTML with inline styles.
3. Drive the HTML through a headless Chromium (Playwright) at 1920×1080
   with device_scale_factor=2 for a crisp >=1600 px-wide PNG suitable
   for a portfolio OG.

Invoked by ``make cli-demo`` and by CI's screenshot job; can also be
run directly: ``uv run python scripts/generate_cli_screenshot.py``.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from hfao.cli import _ingest_one, _synthetic_span, render_dashboard
from hfao.config import HFAOConfig
from hfao.ingest.body_offload import BodyOffloader, LocalBodyStore
from hfao.ingest.redact import RedactionConfig, Redactor
from hfao.storage.duckdb_backend import DuckDBBackend
from rich.console import Console

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "hfao-cli.png"
_WIDTH = 1920
_HEIGHT = 1100
_DSF = 2  # device scale factor → 3840 px effective width, stays crisp when downscaled


def _seed(backend: DuckDBBackend, cfg: HFAOConfig, store_root: Path) -> None:
    redactor = Redactor(RedactionConfig())
    offloader = BodyOffloader(
        LocalBodyStore(store_root),
        threshold_bytes=cfg.body_offload_threshold_bytes,
    )
    rng = random.Random(1337)  # deterministic demo data

    fixtures: list[dict[str, object]] = [
        {"model": "claude-opus-4-7", "operation": "chat",
         "latency_ms": 2480, "cost_usd": 0.0854, "status_fail": False},
        {"model": "claude-sonnet-4-6", "operation": "invoke_agent",
         "latency_ms": 540, "cost_usd": 0.0112, "status_fail": False},
        {"model": "claude-haiku-4-5", "operation": "chat",
         "latency_ms": 180, "cost_usd": 0.0007, "status_fail": False},
        {"model": "gpt-4o", "operation": "execute_tool",
         "latency_ms": 1180, "cost_usd": 0.0214, "status_fail": True},
        {"model": "gpt-4o-mini", "operation": "embeddings",
         "latency_ms": 92, "cost_usd": 0.0003, "status_fail": False},
    ]
    for fx in fixtures:
        span = _synthetic_span(
            project_id=cfg.project,
            model=str(fx["model"]),
            operation=str(fx["operation"]),
            latency_ms=int(fx["latency_ms"]),
            status_fail=bool(fx["status_fail"]),
            rng=rng,
        )
        _ingest_one(
            span,
            backend=backend,
            redactor=redactor,
            offloader=offloader,
            project_id=cfg.project,
            cost_usd=float(fx["cost_usd"]),
        )


def _render_html(cfg: HFAOConfig) -> str:
    # Width is measured in Rich "columns". At 19px monospace the natural
    # 160-col render fits just inside a 1920 CSS px viewport with padding
    # — wider Rich widths wrap the status column off the right edge.
    console = Console(record=True, width=155, color_system="truecolor")
    render_dashboard(console=console, cfg=cfg)
    return console.export_html(inline_styles=True)


def _html_to_png(html: str, out: Path) -> None:
    # Deferred import so importing this script is cheap when only seeding.
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": _WIDTH, "height": _HEIGHT},
            device_scale_factor=_DSF,
        )
        page = context.new_page()
        # Rich exports with light/dark "default" terminal colors; force a dark
        # body so the screenshot matches a real dark-mode terminal.
        styled = html.replace(
            "<head>",
            "<head><style>"
            "html,body{margin:0;padding:0;background:#0b0d12;}"
            "body{padding:28px 28px 20px 28px;}"
            "pre{font-size:19px;line-height:1.35;margin:0;}"
            "</style>",
        )
        page.set_content(styled, wait_until="load")
        # Tight-crop: size the viewport to content height so we don't ship
        # 2000 px of empty black at the bottom of the portfolio PNG.
        content_height = int(page.evaluate("document.body.scrollHeight"))
        page.set_viewport_size({"width": _WIDTH, "height": content_height})
        page.screenshot(path=str(out), clip={
            "x": 0, "y": 0, "width": _WIDTH, "height": content_height,
        })
        context.close()
        browser.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hfao-cli-shot-") as td:
        tmp = Path(td)
        cfg = HFAOConfig(
            project="demo",
            duckdb_path=str(tmp / "hfao.duckdb"),
            bodies_path=str(tmp / "bodies"),
        )
        backend = DuckDBBackend(cfg.duckdb_path)
        backend.init_schema()
        try:
            _seed(backend, cfg, tmp / "bodies")
        finally:
            backend.close()

        html = _render_html(cfg)
        _html_to_png(html, OUTPUT)
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
