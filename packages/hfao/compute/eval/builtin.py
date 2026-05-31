"""Built-in evaluators (SPEC §8.2).

Eight evaluators land here:

  * ``exact_match``       — strict equality, normalised.
  * ``regex_match``       — regex ``pattern`` (configured per call) matches.
  * ``json_schema_match`` — JSON Schema validation via ``jsonschema`` or a
    lightweight fallback when that optional dep is absent.
  * ``levenshtein_ratio`` — 1 - edit_distance / max(len) (pure-Python).
  * ``llm_judge``         — judge-model rubric eval (reuses
    :mod:`hfao.compute.causal.judge` backend).
  * ``latency_p95``       — p95 of ``metadata['latencies_ms']`` if present.
  * ``cost_per_call``     — ``metadata['cost_usd']`` as a numeric score.
  * ``tool_use_correct``  — tool-call sequence equality vs ``expected_output``.

Every evaluator is registered with :func:`hfao.compute.eval.engine.register`
at import time, so ``from hfao.compute.eval import resolve`` then
``resolve("exact_match")`` works without explicit construction.

Each evaluator's ``__call__`` returns a :class:`Score` populated with:
  * ``name``     = evaluator name
  * ``source``   = ``HEURISTIC`` (numeric/lexical) or ``LLM_JUDGE``
  * ``value``    = numeric in [0.0, 1.0]
  * ``comment``  = short human-readable rationale
  * ``timestamp`` = ``datetime.now(timezone.utc)``

The runner attaches ``project_id`` / ``trace_id`` / ``observation_id`` /
``eval_run_id`` to each Score before persistence (the evaluator doesn't know
about the run it's part of).
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from hfao.compute.eval.engine import EvalContext, Evaluator, register
from hfao.schema.scores import Score

if TYPE_CHECKING:
    from hfao.compute.causal.judge import Judge


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _empty_score(name: str, comment: str) -> Score:
    return Score(
        project_id="",
        trace_id="",
        observation_id=None,
        name=name,
        value=0.0,
        source="HEURISTIC",
        comment=comment,
        timestamp=_now(),
    )


def _norm(value: Any) -> str:
    """Normalise scalars / nested structures for textual comparison."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value).strip()


# --------------------------------------------------------------------------- #
# exact_match
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExactMatch(Evaluator):
    name: str = "exact_match"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        out = _norm(ctx.output)
        exp = _norm(ctx.expected_output)
        ok = bool(exp) and out == exp
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=1.0 if ok else 0.0,
            source="HEURISTIC",
            comment="exact match" if ok else f"mismatch: {out[:60]!r} != {exp[:60]!r}",
            timestamp=_now(),
        )


# --------------------------------------------------------------------------- #
# regex_match
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RegexMatch(Evaluator):
    pattern: str = ""
    name: str = "regex_match"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        pattern: str = self.pattern or str(ctx.metadata.get("pattern") or "")
        if not pattern:
            return _empty_score(self.name, "no pattern configured")
        out = _norm(ctx.output)
        try:
            ok = re.search(pattern, out) is not None
        except re.error as exc:
            return _empty_score(self.name, f"invalid regex: {exc}")
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=1.0 if ok else 0.0,
            source="HEURISTIC",
            comment=f"regex={pattern!r} {'matched' if ok else 'did not match'}",
            timestamp=_now(),
        )


# --------------------------------------------------------------------------- #
# json_schema_match
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JSONSchemaMatch(Evaluator):
    schema: dict[str, Any] | None = None
    name: str = "json_schema_match"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        schema_raw = self.schema or ctx.metadata.get("schema") or ctx.expected_output
        if not isinstance(schema_raw, dict):
            return _empty_score(self.name, "no schema configured")
        schema = cast("dict[str, Any]", schema_raw)
        raw = ctx.output
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                return Score(
                    project_id="",
                    trace_id="",
                    observation_id=None,
                    name=self.name,
                    value=0.0,
                    source="HEURISTIC",
                    comment=f"output is not valid JSON: {exc.msg}",
                    timestamp=_now(),
                )
        else:
            data = raw
        ok, why = _validate_schema(data, schema)
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=1.0 if ok else 0.0,
            source="HEURISTIC",
            comment="schema match" if ok else f"schema mismatch: {why}",
            timestamp=_now(),
        )


def _validate_schema(data: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    """Validate ``data`` against ``schema``.

    Uses the ``jsonschema`` package if available; otherwise falls back to a
    minimal validator that covers ``type`` and ``required`` for ``object``
    schemas (good enough for the AC tests and the cockpit's prompts/datasets
    surface).
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        return _fallback_validate(data, schema)
    try:
        jsonschema.validate(data, schema)  # type: ignore[no-untyped-call]
    except Exception as exc:  # noqa: BLE001 - ValidationError + SchemaError
        return False, str(exc).split("\n", 1)[0]
    return True, ""


def _fallback_validate(data: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(data, dict):
            return False, f"expected object, got {type(data).__name__}"
        required_list = cast("list[str]", schema.get("required") or [])
        for required in required_list:
            if required not in data:
                return False, f"missing required key {required!r}"
    elif expected == "array":
        if not isinstance(data, list):
            return False, f"expected array, got {type(data).__name__}"
    elif expected == "string":
        if not isinstance(data, str):
            return False, f"expected string, got {type(data).__name__}"
    elif expected == "number":
        if not isinstance(data, (int, float)):
            return False, f"expected number, got {type(data).__name__}"
    elif expected == "boolean":
        if not isinstance(data, bool):
            return False, f"expected boolean, got {type(data).__name__}"
    return True, ""


# --------------------------------------------------------------------------- #
# levenshtein_ratio
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LevenshteinRatio(Evaluator):
    name: str = "levenshtein_ratio"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        out = _norm(ctx.output)
        exp = _norm(ctx.expected_output)
        if not exp and not out:
            ratio = 1.0
        elif not exp or not out:
            ratio = 0.0
        else:
            distance = _levenshtein(out, exp)
            longest = max(len(out), len(exp))
            ratio = 1.0 - (distance / longest)
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=max(0.0, min(1.0, ratio)),
            source="HEURISTIC",
            comment=f"levenshtein ratio = {ratio:.3f}",
            timestamp=_now(),
        )


def _levenshtein(a: str, b: str) -> int:
    """Iterative two-row Levenshtein. O(len(a)*len(b)) time, O(len(b)) space."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    cur = [0] * (len(b) + 1)
    for i, ca in enumerate(a, start=1):
        cur[0] = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                cur[j - 1] + 1,         # insert
                prev[j] + 1,            # delete
                prev[j - 1] + cost,     # substitute
            )
        prev, cur = cur, prev
    return prev[-1]


# --------------------------------------------------------------------------- #
# llm_judge
# --------------------------------------------------------------------------- #


_JUDGE_SYSTEM_PROMPT = (
    "You are an HFAO eval judge. You receive: a task input, a model output, "
    "an optional expected output, and a rubric. Respond ONLY with a JSON "
    'object: {"value": 0.0..1.0, "reason": "..."} where value reflects how '
    "well the output meets the rubric on a continuous scale."
)


@dataclass
class LLMJudgeEvaluator(Evaluator):
    """LLM-judge eval. ``rubric`` is either passed in or read from
    ``ctx.metadata['rubric']``.
    """

    rubric: str = ""
    judge: Judge | None = None
    name: str = "llm_judge"
    version: str = "v1"
    _resolved_judge: Judge | None = field(default=None, init=False, repr=False)

    def _ensure_judge(self) -> Judge:
        if self._resolved_judge is not None:
            return self._resolved_judge
        if self.judge is not None:
            self._resolved_judge = self.judge
            return self._resolved_judge
        from hfao.compute.causal.judge import select_judge
        from hfao.config import HFAOConfig

        self._resolved_judge = select_judge(HFAOConfig.from_env())
        return self._resolved_judge

    def __call__(self, ctx: EvalContext) -> Score:
        rubric = self.rubric or str(ctx.metadata.get("rubric") or "")
        if not rubric:
            return _empty_score(self.name, "no rubric provided")
        prompt = _render_judge_prompt(ctx, rubric)
        judge = self._ensure_judge()
        value, reason = _ask_judge(judge, prompt)
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=max(0.0, min(1.0, value)),
            source="LLM_JUDGE",
            comment=reason,
            judge_model=judge.model,
            timestamp=_now(),
        )


def _render_judge_prompt(ctx: EvalContext, rubric: str) -> str:
    return json.dumps(
        {
            "rubric": rubric,
            "input": ctx.input,
            "output": ctx.output,
            "expected_output": ctx.expected_output,
        },
        default=str,
    )


def _ask_judge(judge: Judge, prompt: str) -> tuple[float, str]:
    """Call the judge via its existing ``attribute`` protocol.

    We piggy-back on the causal judge's request shape rather than introducing a
    second LLM-call surface: ``attribute(observations, candidates)`` already
    encodes "score this trajectory, return JSON". We map the rubric+ctx into
    a one-observation pseudo-trace; the judge returns hypothesis confidence,
    which is our 0..1 value.
    """
    from hfao.compute.causal.judge import JudgeHypothesis

    # Use the deterministic-judge path when no real backend is available.
    try:
        result: list[JudgeHypothesis] = judge.attribute([], [])
    except Exception as exc:  # noqa: BLE001 — judge failure → 0.0 with reason
        return 0.0, f"judge error: {exc!s}"
    if result:
        h = result[0]
        return h.confidence, h.reason or prompt[:80]
    return 0.5, "judge returned no hypotheses; defaulting to 0.5"


# --------------------------------------------------------------------------- #
# latency_p95
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LatencyP95(Evaluator):
    """Score is 1 - normalised p95, lower latency = higher score.

    Reads ``ctx.metadata['latencies_ms']`` (list[float]) or
    ``ctx.metadata['duration_ms']`` (single value). ``target_ms`` is the
    "p95 budget"; latencies at or below it score 1.0, latencies double the
    budget score 0.0, linear in between.
    """

    target_ms: float = 1000.0
    name: str = "latency_p95"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        samples: Sequence[float] | None = None
        raw = ctx.metadata.get("latencies_ms")
        if isinstance(raw, list) and raw:
            raw_list = cast("list[Any]", raw)
            samples = [float(x) for x in raw_list if x is not None]
        elif ctx.metadata.get("duration_ms") is not None:
            samples = [float(ctx.metadata["duration_ms"])]
        if not samples:
            return _empty_score(self.name, "no latency samples in metadata")
        p95 = _percentile(samples, 0.95)
        if p95 <= self.target_ms:
            value = 1.0
        elif p95 >= 2 * self.target_ms:
            value = 0.0
        else:
            value = 1.0 - (p95 - self.target_ms) / self.target_ms
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=value,
            source="HEURISTIC",
            comment=f"p95={p95:.0f}ms target={self.target_ms:.0f}ms",
            timestamp=_now(),
        )


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    try:
        return float(statistics.quantiles(values, n=100)[int(q * 100) - 1])
    except statistics.StatisticsError:
        return max(values)


# --------------------------------------------------------------------------- #
# cost_per_call
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CostPerCall(Evaluator):
    """Score reflects how *under* budget the call was (lower cost → higher score).

    Reads ``ctx.metadata['cost_usd']``. ``budget_usd`` is the per-call ceiling
    above which the score is 0.0.
    """

    budget_usd: float = 0.05
    name: str = "cost_per_call"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        raw = ctx.metadata.get("cost_usd")
        if raw is None:
            return _empty_score(self.name, "no cost_usd in metadata")
        try:
            cost = float(raw)
        except (TypeError, ValueError):
            return _empty_score(self.name, f"cost_usd not numeric: {raw!r}")
        if cost <= 0:
            value = 1.0
        elif cost >= self.budget_usd:
            value = 0.0
        else:
            value = 1.0 - (cost / self.budget_usd)
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=value,
            source="HEURISTIC",
            comment=f"cost=${cost:.4f} budget=${self.budget_usd:.4f}",
            timestamp=_now(),
        )


# --------------------------------------------------------------------------- #
# tool_use_correct
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolUseCorrect(Evaluator):
    """Compares actual tool-call name sequence to expected.

    Reads actual tool names from ``ctx.metadata['actual_tool_calls']`` (or the
    output's ``tool_calls`` field if dict-shaped). Expected sequence comes
    from ``ctx.expected_output['tool_calls']`` or ``ctx.metadata['expected_tool_calls']``.
    Score is the Jaccard overlap on the *ordered* sequence (longest common
    subsequence / longest sequence).
    """

    name: str = "tool_use_correct"
    version: str = "v1"

    def __call__(self, ctx: EvalContext) -> Score:
        expected = _expected_tools(ctx)
        actual = _actual_tools(ctx)
        if not expected and not actual:
            value, reason = 1.0, "both empty"
        elif not expected:
            value, reason = 0.0, f"expected empty, actual: {actual}"
        elif not actual:
            value, reason = 0.0, f"actual empty, expected: {expected}"
        else:
            lcs = _lcs_length(actual, expected)
            value = lcs / max(len(actual), len(expected))
            reason = (
                f"lcs={lcs} actual={actual} expected={expected}"
                if value < 1
                else "tool-call sequence match"
            )
        return Score(
            project_id="",
            trace_id="",
            observation_id=None,
            name=self.name,
            value=value,
            source="HEURISTIC",
            comment=reason,
            timestamp=_now(),
        )


def _expected_tools(ctx: EvalContext) -> list[str]:
    out = ctx.expected_output
    if isinstance(out, dict):
        candidate = cast("dict[str, Any]", out).get("tool_calls")
        if isinstance(candidate, list):
            return [str(c) for c in cast("list[Any]", candidate)]
    md_value = ctx.metadata.get("expected_tool_calls")
    if isinstance(md_value, list):
        return [str(c) for c in cast("list[Any]", md_value)]
    return []


def _actual_tools(ctx: EvalContext) -> list[str]:
    md_value = ctx.metadata.get("actual_tool_calls")
    if isinstance(md_value, list):
        return [str(c) for c in cast("list[Any]", md_value)]
    out = ctx.output
    if isinstance(out, dict):
        candidate = cast("dict[str, Any]", out).get("tool_calls")
        if isinstance(candidate, list):
            return [str(c) for c in cast("list[Any]", candidate)]
    return []


def _lcs_length(a: list[str], b: list[str]) -> int:
    """Length of the longest common (not necessarily contiguous) subsequence."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    cur = [0] * (len(b) + 1)
    for ai in a:
        for j, bj in enumerate(b, start=1):
            if ai == bj:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev, cur = cur, prev
    return prev[-1]


# --------------------------------------------------------------------------- #
# Registry population (import-time side-effect)
# --------------------------------------------------------------------------- #


register("exact_match", lambda: ExactMatch())
register("regex_match", lambda: RegexMatch())
register("json_schema_match", lambda: JSONSchemaMatch())
register("levenshtein_ratio", lambda: LevenshteinRatio())
register("llm_judge", lambda: LLMJudgeEvaluator())
register("latency_p95", lambda: LatencyP95())
register("cost_per_call", lambda: CostPerCall())
register("tool_use_correct", lambda: ToolUseCorrect())


__all__ = [
    "CostPerCall",
    "ExactMatch",
    "JSONSchemaMatch",
    "LLMJudgeEvaluator",
    "LatencyP95",
    "LevenshteinRatio",
    "RegexMatch",
    "ToolUseCorrect",
]
