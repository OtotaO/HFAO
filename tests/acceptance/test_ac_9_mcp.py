"""AC §9 acceptance tests — HFAO-as-MCP server.

Covers §9.4:

    - test_mcp_streamable_http_handshake
    - test_list_traces_requires_auth
    - test_score_observation_blocked_in_readonly
    - test_get_causal_attribution_returns_replay_flag
    - test_workspace_isolation_enforced
    - test_mcp_resource_uri_resolves

The tests run a real uvicorn server in a background thread and connect with
``fastmcp.Client`` over Streamable HTTP (§9.1), so per-request ``Authorization``
header propagation and workspace isolation are exercised end-to-end. Async
calls are driven with ``asyncio.run`` to avoid a pytest-async plugin dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from hfao.config import HFAOConfig
from hfao.mcp_server.server import MCP_PATH, build_asgi_app
from hfao.schema.causal import CausalEdge
from hfao.schema.events import CostBreakdown, Observation, TokenUsage
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend

_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)


class Env:
    """A configured control plane + backend with two isolated workspaces."""

    def __init__(self) -> None:
        self.control = ControlPlane("sqlite:///:memory:")
        self.control.init_schema()
        ws_a = self.control.create_workspace(slug="acme", name="Acme")
        ws_b = self.control.create_workspace(slug="globex", name="Globex")
        self.project_a = self.control.create_project(
            workspace_id=ws_a["id"], slug="demo", name="Demo"
        )["id"]
        self.project_b = self.control.create_project(
            workspace_id=ws_b["id"], slug="other", name="Other"
        )["id"]
        self.key_a, _ = self.control.issue_api_key(
            workspace_id=ws_a["id"], role="admin", name="a"
        )
        self.key_b, _ = self.control.issue_api_key(
            workspace_id=ws_b["id"], role="admin", name="b"
        )

        self.backend = DuckDBBackend(":memory:")
        self.backend.init_schema()
        self.backend.write_events(
            [
                Observation(
                    project_id=self.project_a,
                    trace_id="t1",
                    observation_id="o1",
                    name="generate",
                    type="GENERATION",
                    start_time=_NOW,
                    end_time=_NOW + timedelta(milliseconds=5),
                    duration_ms=5,
                    ingested_at=_NOW,
                    status="error",
                    usage=TokenUsage(total_tokens=42),
                    cost=CostBreakdown(total_cost_usd=0.01),
                    event_version=1,
                )
            ]
        )
        self.backend.write_causal_edges(
            [
                CausalEdge(
                    project_id=self.project_a,
                    trace_id="t1",
                    source_observation_id="o1",
                    target_observation_id="o1",
                    edge_type="DECISIVE_ERROR",
                    confidence=0.82,
                    method="LLM_JUDGE",
                    evidence="malformed tool arguments led to the failure",
                    replay_supported=True,
                    judge_model="claude-haiku-4-5",
                    computed_at=_NOW,
                )
            ]
        )


@contextlib.contextmanager
def _running(app: Any) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("MCP server did not start in time")
    try:
        yield f"http://127.0.0.1:{port}{MCP_PATH}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def env() -> Env:
    return Env()


@pytest.fixture
def server_url(env: Env) -> Iterator[str]:
    app = build_asgi_app(HFAOConfig(), env.backend, env.control)
    with _running(app) as url:
        yield url


def _client(url: str, key: str | None) -> Client:
    headers = {"Authorization": f"Bearer {key}"} if key else None
    return Client(StreamableHttpTransport(url=url, headers=headers))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #


def test_mcp_streamable_http_handshake(server_url: str) -> None:
    async def go() -> list[str]:
        async with _client(server_url, None) as client:
            return sorted(t.name for t in await client.list_tools())

    names = _run(go())
    expected = {
        "list_traces",
        "get_trace",
        "search_traces",
        "get_causal_attribution",
        "list_decisive_errors",
        "compare_runs",
        "run_eval",
        "get_prompt",
        "list_prompts",
        "score_observation",
        "get_cost_by",
    }
    assert expected.issubset(set(names))


def test_list_traces_requires_auth(server_url: str, env: Env) -> None:
    async def go() -> None:
        async with _client(server_url, None) as client:
            await client.call_tool("list_traces", {"project": env.project_a})

    with pytest.raises(Exception) as exc:  # noqa: PT011 - fastmcp ToolError
        _run(go())
    assert "authentication" in str(exc.value).lower()


def test_get_causal_attribution_returns_replay_flag(
    server_url: str, env: Env
) -> None:
    async def go() -> dict[str, Any]:
        async with _client(server_url, env.key_a) as client:
            result = await client.call_tool(
                "get_causal_attribution",
                {"project": env.project_a, "trace_id": "t1"},
            )
            return result.data

    data = _run(go())
    assert "hypotheses" in data["disclaimer"].lower()  # "hypotheses, not verdicts"
    top = data["hypotheses"][0]
    assert top["replay_supported"] is True
    assert top["method"] == "LLM_JUDGE"
    assert "confidence" in top


def test_score_observation_blocked_in_readonly(env: Env) -> None:
    read_only = HFAOConfig(mcp_read_only=True)
    app = build_asgi_app(read_only, env.backend, env.control)

    async def go(url: str) -> None:
        async with _client(url, env.key_a) as client:
            await client.call_tool(
                "score_observation",
                {
                    "project": env.project_a,
                    "trace_id": "t1",
                    "observation_id": "o1",
                    "name": "quality",
                    "value": 1.0,
                },
            )

    with _running(app) as url, pytest.raises(Exception) as exc:  # noqa: PT011
        _run(go(url))
    assert "read-only" in str(exc.value).lower()


def test_score_observation_writes_when_writable(server_url: str, env: Env) -> None:
    async def go() -> dict[str, Any]:
        async with _client(server_url, env.key_a) as client:
            result = await client.call_tool(
                "score_observation",
                {
                    "project": env.project_a,
                    "trace_id": "t1",
                    "observation_id": "o1",
                    "name": "quality",
                    "value": 0.5,
                    "comment": "ok",
                },
            )
            return result.data

    data = _run(go())
    assert data["name"] == "quality"
    assert data["source"] == "ANNOTATION"
    persisted = env.backend.get_scores(env.project_a, "t1")
    assert any(s.name == "quality" and s.value == 0.5 for s in persisted)


def test_workspace_isolation_enforced(server_url: str, env: Env) -> None:
    async def go() -> None:
        # key_a belongs to workspace "acme"; project_b belongs to "globex".
        async with _client(server_url, env.key_a) as client:
            await client.call_tool("get_trace", {"project": env.project_b, "trace_id": "t1"})

    with pytest.raises(Exception) as exc:  # noqa: PT011 - fastmcp ToolError
        _run(go())
    assert "not in workspace" in str(exc.value).lower()


def test_mcp_resource_uri_resolves(server_url: str, env: Env) -> None:
    async def go() -> str:
        async with _client(server_url, env.key_a) as client:
            contents = await client.read_resource(
                f"hfao://traces/{env.project_a}/t1"
            )
            return contents[0].text  # type: ignore[union-attr]

    text = _run(go())
    assert "observations" in text
    assert "o1" in text
