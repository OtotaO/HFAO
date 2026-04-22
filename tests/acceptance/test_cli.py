"""AC — ``hfao`` CLI end-to-end.

Verifies the portfolio flow: ``hfao ingest send`` emits a trace_id, and
the same trace_id is reachable via ``hfao query`` in the next invocation
(same DuckDB path, same process family). No Granian server required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hfao.cli import app
from typer.testing import CliRunner

_TRACE_RE = re.compile(r"trace_id=([0-9a-f]{32})")


@pytest.fixture
def runner() -> CliRunner:
    # mix_stderr=False so Typer's stderr output (if any) doesn't pollute stdout.
    return CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFAO_DUCKDB_PATH", str(tmp_path / "hfao.duckdb"))
    monkeypatch.setenv("HFAO_BODIES_PATH", str(tmp_path / "bodies"))
    monkeypatch.setenv("HFAO_PROJECT", "cli-test")


def test_hfao_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("hfao ")


def test_help_lists_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("query", "dashboard", "ingest"):
        assert sub in result.stdout


def test_ingest_send_then_query_roundtrip(
    runner: CliRunner, env: None
) -> None:
    _ = env
    send = runner.invoke(app, ["ingest", "send", "--count", "1", "--seed", "11"])
    assert send.exit_code == 0, send.stdout
    m = _TRACE_RE.search(send.stdout)
    assert m is not None, f"no trace_id in: {send.stdout!r}"
    trace_id = m.group(1)

    q = runner.invoke(app, ["query", "5"])
    assert q.exit_code == 0, q.stdout
    # Rich truncates trace_id in the table; match on a safe prefix.
    assert trace_id[:16] in q.stdout


def test_dashboard_renders_with_data(runner: CliRunner, env: None) -> None:
    _ = env
    seed = runner.invoke(
        app, ["ingest", "send", "--count", "3", "--vary", "--seed", "3"]
    )
    assert seed.exit_code == 0, seed.stdout

    dash = runner.invoke(app, ["dashboard"])
    assert dash.exit_code == 0, dash.stdout
    out = dash.stdout
    assert "HFAO Observatory" in out
    assert "Storage" in out
    assert "Ingest (last 5 min)" in out
    assert "Recent traces" in out
