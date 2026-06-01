"""Eval runner — offline batch + online sampler + CLI gate (SPEC §8.2).

Three entry points:

  * :func:`run_offline` — iterate every item in a dataset, optionally invoke
    a runtime to produce ``output`` (HTTP or in-process callable), apply each
    evaluator, persist Scores, return a summary + gate result.
  * :func:`should_sample_online` — runtime-side helper for ``on_trace_close``
    triggers. Returns ``True`` when a closing trace should be eval-sampled at
    the configured rate (and matches an optional ``where_sql`` filter).
  * :func:`run_eval` — the MCP tool's entry point. Resolves a dataset by name,
    runs offline, returns a dict the FastMCP serializer can ship.

Plus :func:`parse_gate` and :func:`evaluate_gate` for the CLI's
``--gate "exact_match>=0.9"`` regression guard.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from hfao.compute.eval.engine import EvalContext, Evaluator, resolve
from hfao.schema.eval_runs import EvalRun
from hfao.schema.scores import Score

if TYPE_CHECKING:
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane


# --------------------------------------------------------------------------- #
# Gate parsing
# --------------------------------------------------------------------------- #

_GATE_OPERATORS = (">=", "<=", "==", "!=", ">", "<")
_GATE_RE = re.compile(
    r"^\s*([A-Za-z_][\w]*)\s*(>=|<=|==|!=|>|<)\s*([-+]?\d*\.?\d+)\s*$"
)


@dataclass(frozen=True)
class Gate:
    name: str
    op: str
    threshold: float

    def check(self, value: float) -> bool:
        return _OPS[self.op](value, self.threshold)


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def parse_gate(expression: str) -> Gate:
    """Parse a gate like ``exact_match>=0.9`` into a :class:`Gate`."""
    match = _GATE_RE.match(expression)
    if not match:
        raise ValueError(
            f"invalid gate {expression!r}: expected '<name><op><threshold>' "
            f"with op in {_GATE_OPERATORS}"
        )
    name, op, threshold_raw = match.group(1), match.group(2), match.group(3)
    return Gate(name=name, op=op, threshold=float(threshold_raw))


def evaluate_gate(gate: Gate, summary: dict[str, float]) -> tuple[bool, str]:
    """Return ``(passed, reason)`` for a gate against a run summary."""
    if gate.name not in summary:
        return False, f"summary missing metric {gate.name!r}"
    actual = summary[gate.name]
    passed = gate.check(actual)
    cmp_text = f"{actual:.4f} {gate.op} {gate.threshold:.4f}"
    return passed, f"{gate.name}: {cmp_text} → {'pass' if passed else 'fail'}"


# --------------------------------------------------------------------------- #
# Runtime callable
# --------------------------------------------------------------------------- #


Runtime = Callable[[EvalContext], Any]


def http_runtime(url: str, *, timeout: float = 30.0) -> Runtime:
    """Build a runtime that POSTs each input to ``url`` and returns the output.

    Body shape: ``{"input": ..., "metadata": ...}``. The runtime is expected
    to reply with ``{"output": ...}`` (JSON); the value of ``output`` is
    returned as-is. Any non-200 response returns ``None`` (the evaluators
    handle missing output gracefully).
    """

    def _call(ctx: EvalContext) -> Any:
        try:
            resp = httpx.post(
                url,
                json={"input": ctx.input, "metadata": ctx.metadata},
                timeout=timeout,
            )
            if resp.status_code != 200:
                return None
            body: dict[str, Any] = resp.json()
            return body.get("output")
        except httpx.HTTPError:
            return None

    return _call


def echo_runtime() -> Runtime:
    """Trivial runtime: returns the input unchanged. Useful for testing
    metadata-only evaluators (latency / cost / tool_use)."""
    return lambda ctx: ctx.input


# --------------------------------------------------------------------------- #
# Offline runner
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OfflineResult:
    """Return shape for :func:`run_offline`."""

    eval_run: EvalRun
    scores: list[Score]


def run_offline(
    *,
    backend: StorageBackend,
    control: ControlPlane,
    project_id: str,
    dataset_id: str,
    evaluators: list[str],
    runtime: Runtime | str | None = None,
    gate: Gate | None = None,
    persist: bool = True,
) -> OfflineResult:
    """Run every dataset item through ``runtime`` then each evaluator.

    The Scores produced carry ``eval_run_id`` so the cockpit's read-only Evals
    tab (PR #10) can aggregate them. The :class:`EvalRun` itself is **not**
    persisted in v1 — that's the Week-8 ``eval_runs`` table; for now we
    return it for the caller to surface in-memory.

    ``runtime`` may be a callable, an HTTP URL string, or ``None`` (defaults
    to :func:`echo_runtime`).
    """
    if not evaluators:
        raise ValueError("at least one evaluator required")
    if isinstance(runtime, str):
        runtime_fn: Runtime = http_runtime(runtime)
        runtime_url: str | None = runtime
    elif runtime is None:
        runtime_fn = echo_runtime()
        runtime_url = None
    else:
        runtime_fn = runtime
        runtime_url = None

    resolved: list[Evaluator] = [resolve(name) for name in evaluators]
    items = control.list_dataset_items(project_id=project_id, dataset_id=dataset_id)
    started = datetime.now(timezone.utc)
    run_id = f"er_{uuid.uuid4().hex[:24]}"

    all_scores: list[Score] = []
    per_metric: dict[str, list[float]] = {ev.name: [] for ev in resolved}

    for item in items:
        ctx = EvalContext(
            input=maybe_json(item["input"]),
            output=None,
            expected_output=maybe_json(item.get("expected_output")),
            metadata=maybe_json_dict(item.get("metadata") or "{}") | {
                "dataset_item_id": item["id"],
            },
        )
        try:
            output = runtime_fn(ctx)
        except Exception as exc:  # noqa: BLE001 — record but keep going
            output = None
            ctx = EvalContext(
                input=ctx.input,
                output=None,
                expected_output=ctx.expected_output,
                metadata={**ctx.metadata, "runtime_error": str(exc)},
            )
        ctx = EvalContext(
            input=ctx.input,
            output=output,
            expected_output=ctx.expected_output,
            metadata=ctx.metadata,
        )
        for evaluator in resolved:
            raw = evaluator(ctx)
            score = _attach_run_fields(raw, project_id, run_id, item)
            all_scores.append(score)
            if score.value is not None:
                per_metric[evaluator.name].append(score.value)

    summary = {
        name: (sum(vs) / len(vs)) if vs else 0.0
        for name, vs in per_metric.items()
    }
    gate_passed: bool | None = None
    if gate is not None:
        gate_passed, _ = evaluate_gate(gate, summary)

    eval_run = EvalRun(
        id=run_id,
        project_id=project_id,
        dataset_id=dataset_id,
        evaluators=list(evaluators),
        status="done" if gate_passed is not False else "failed",
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        summary=summary,
        gate_expression=f"{gate.name}{gate.op}{gate.threshold}" if gate else None,
        gate_passed=gate_passed,
        runtime_url=runtime_url,
        sample_count=len(items),
    )

    if persist and all_scores:
        backend.write_scores(all_scores)

    return OfflineResult(eval_run=eval_run, scores=all_scores)


def maybe_json(raw: Any) -> Any:
    """Parse JSON when ``raw`` is a string that looks like JSON; else return as-is."""
    if not isinstance(raw, str):
        return raw
    stripped = raw.strip()
    if not stripped or stripped[0] not in "{[\"":
        return raw
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return raw


def maybe_json_dict(raw: Any) -> dict[str, Any]:
    from typing import cast as _cast

    parsed = maybe_json(raw)
    if isinstance(parsed, dict):
        return _cast("dict[str, Any]", parsed)
    return {}


def _attach_run_fields(
    score: Score, project_id: str, run_id: str, item: dict[str, Any]
) -> Score:
    """Stamp project / trace / eval_run_id onto an evaluator-emitted score."""
    return Score(
        project_id=project_id,
        trace_id=item.get("source_trace_id") or item["id"],
        observation_id=item.get("source_observation_id"),
        name=score.name,
        value=score.value,
        string_value=score.string_value,
        source=score.source,
        comment=score.comment,
        judge_model=score.judge_model,
        calibration_bias=score.calibration_bias,
        timestamp=score.timestamp,
        annotator_id=score.annotator_id,
        eval_run_id=run_id,
    )


# --------------------------------------------------------------------------- #
# Online sampler
# --------------------------------------------------------------------------- #


_ALPHABET_LEN = 16
_HEX = re.compile(r"[0-9a-f]")


def should_sample_online(
    *,
    trace_id: str,
    sample_pct: float,
) -> bool:
    """Deterministic ``trace_id``-based sampler.

    ``sample_pct`` in [0, 100]. Uses the first 6 hex chars of ``trace_id`` as
    a uniform random key, so the same trace either is or isn't sampled across
    workers without coordination. Non-hex trace_ids fall back to ``hash``.
    """
    if sample_pct <= 0:
        return False
    if sample_pct >= 100:
        return True
    head = "".join(c for c in trace_id.lower()[:8] if _HEX.match(c))[:6]
    bucket = (
        int(head, 16) % 10000
        if len(head) >= 6
        else (hash(trace_id) & 0xFFFFFFFF) % 10000
    )
    threshold = int((sample_pct / 100.0) * 10000)
    return bucket < threshold


# --------------------------------------------------------------------------- #
# MCP entry point (resolves dataset by name → id then calls run_offline)
# --------------------------------------------------------------------------- #


def run_eval(
    *,
    project: str,
    dataset: str,
    evaluators: list[str],
    runtime_url: str | None = None,
    backend: StorageBackend | None = None,
    control: ControlPlane | None = None,
    gate_expression: str | None = None,
) -> dict[str, Any]:
    """Entry point used by the §9.2 MCP ``run_eval`` tool.

    Either both ``backend`` and ``control`` are passed (in-process call), or
    both are ``None`` and we open them from environment config (the MCP server
    does the latter via its dependency injection — see
    :mod:`hfao.mcp_server.tools`).
    """
    from hfao.config import HFAOConfig
    from hfao.storage.clickhouse_backend import ClickHouseBackend
    from hfao.storage.control_plane import ControlPlane as _ControlPlane
    from hfao.storage.duckdb_backend import DuckDBBackend

    own_backend = backend is None
    own_control = control is None
    cfg = HFAOConfig.from_env()
    if backend is None:
        if cfg.backend == "clickhouse":
            if not cfg.clickhouse_dsn:
                raise RuntimeError("HFAO_BACKEND=clickhouse requires HFAO_CLICKHOUSE_DSN")
            backend = ClickHouseBackend(cfg.clickhouse_dsn)
        else:
            backend = DuckDBBackend(cfg.duckdb_path)
        backend.init_schema()
    if control is None:
        control = _ControlPlane(cfg.control_plane_dsn)
        control.init_schema()
    resolved_backend: StorageBackend = backend
    resolved_control: ControlPlane = control
    try:
        dataset_id = _resolve_dataset(resolved_control, project, dataset)
        gate = parse_gate(gate_expression) if gate_expression else None
        result = run_offline(
            backend=resolved_backend,
            control=resolved_control,
            project_id=project,
            dataset_id=dataset_id,
            evaluators=evaluators,
            runtime=runtime_url,
            gate=gate,
        )
        return _eval_run_to_dict(result.eval_run)
    finally:
        if own_backend:
            resolved_backend.close()
        if own_control:
            resolved_control.close()


def _resolve_dataset(
    control: ControlPlane, project_id: str, dataset: str
) -> str:
    """Resolve a dataset by id (preferred) or by name. Raises ``KeyError``."""
    try:
        return control.get_dataset(project_id=project_id, dataset_id=dataset)["id"]
    except KeyError:
        pass
    for ds in control.list_datasets(project_id=project_id):
        if ds["name"] == dataset:
            return ds["id"]
    raise KeyError(f"no dataset {dataset!r} in project {project_id!r}")


def _eval_run_to_dict(run: EvalRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "dataset_id": run.dataset_id,
        "evaluators": list(run.evaluators),
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "summary": dict(run.summary),
        "gate_expression": run.gate_expression,
        "gate_passed": run.gate_passed,
        "runtime_url": run.runtime_url,
        "sample_count": run.sample_count,
    }


# --------------------------------------------------------------------------- #
# Backwards-compatible kwargs alias used by the MCP tool that lives in
# hfao.mcp_server.tools (which calls run_eval(project=, dataset=, evaluators=,
# runtime_url=)).
# --------------------------------------------------------------------------- #


def filter_traces_for_eval(
    summaries: Iterable[dict[str, Any]], *, where_sql: str
) -> list[dict[str, Any]]:
    """Lightweight Python-side filter used by online-eval triggers when the
    backend's where_sql validation rejects user-supplied SQL.

    This is intentionally minimal — full SQL filtering goes through the backend
    (``StorageBackend.list_traces(..., where_sql=...)``). The Python fallback
    only handles equality predicates on top-level fields, e.g.
    ``status = 'error'``. Anything else returns the input list unchanged.
    """
    eq = re.match(r"^\s*(\w+)\s*=\s*'([^']*)'\s*$", where_sql or "")
    if not eq:
        return list(summaries)
    key, value = eq.group(1), eq.group(2)
    return [s for s in summaries if str(s.get(key)) == value]


__all__ = [
    "Gate",
    "OfflineResult",
    "Runtime",
    "echo_runtime",
    "evaluate_gate",
    "filter_traces_for_eval",
    "http_runtime",
    "parse_gate",
    "run_eval",
    "run_offline",
    "should_sample_online",
]
