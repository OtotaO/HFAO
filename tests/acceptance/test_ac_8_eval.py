"""AC §8 acceptance tests — eval-engine subset.

Covers the §8.5 lines that pertain to the Phase-1 eval engine:

    - test_eval_run_offline_writes_scores
    - test_eval_run_gate_exits_nonzero

plus per-evaluator coverage for the eight built-ins per §8.2, the online
sampler, the calibration helpers, and the CLI ``hfao eval run`` integration.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hfao.compute.causal.judge import DeterministicJudge, JudgeHypothesis
from hfao.compute.eval import (
    EvalContext,
    apply_bias,
    compute_bias,
    evaluate_gate,
    pair_scores,
    parse_gate,
    register,
    registry,
    resolve,
    run_offline,
    should_sample_online,
)
from hfao.compute.eval.builtin import (
    CostPerCall,
    ExactMatch,
    JSONSchemaMatch,
    LatencyP95,
    LevenshteinRatio,
    LLMJudgeEvaluator,
    RegexMatch,
    ToolUseCorrect,
)
from hfao.schema.scores import Score
from hfao.storage.control_plane import ControlPlane
from hfao.storage.duckdb_backend import DuckDBBackend
from typer.testing import CliRunner

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[DuckDBBackend]:
    b = DuckDBBackend(str(tmp_path / "hfao.duckdb"))
    b.init_schema()
    yield b
    b.close()


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlPlane]:
    c = ControlPlane(f"sqlite:///{tmp_path / 'cp.db'}")
    c.init_schema()
    yield c
    c.close()


@pytest.fixture
def seeded(
    control: ControlPlane,
) -> tuple[str, str]:
    """Seed a project + dataset with two items, return (project_id, dataset_id)."""
    ws = control.create_workspace(slug="acme", name="Acme")
    proj = control.create_project(workspace_id=ws["id"], slug="demo", name="Demo")
    ds = control.create_dataset(project_id=proj["id"], name="goldens")
    control.add_dataset_item(
        project_id=proj["id"],
        dataset_id=ds["id"],
        input="2+2",
        expected_output="4",
        metadata={"cost_usd": "0.01", "duration_ms": "300"},
    )
    control.add_dataset_item(
        project_id=proj["id"],
        dataset_id=ds["id"],
        input="capital of France",
        expected_output="Paris",
        metadata={"cost_usd": "0.02", "duration_ms": "500"},
    )
    return proj["id"], ds["id"]


# --------------------------------------------------------------------------- #
# Built-in evaluators — one focused test per evaluator
# --------------------------------------------------------------------------- #


def test_exact_match_evaluator() -> None:
    ev = ExactMatch()
    assert ev(EvalContext(input="x", output="hi", expected_output="hi")).value == 1.0
    assert ev(EvalContext(input="x", output="hi", expected_output="bye")).value == 0.0
    # Empty expected → no match.
    assert ev(EvalContext(input="x", output="hi")).value == 0.0


def test_regex_match_evaluator() -> None:
    ev = RegexMatch(pattern=r"\b4\b")
    assert ev(EvalContext(input="2+2", output="the answer is 4")).value == 1.0
    assert ev(EvalContext(input="2+2", output="no match here")).value == 0.0
    # Pattern from metadata.
    ev2 = RegexMatch()
    ctx = EvalContext(input="x", output="abc 123", metadata={"pattern": r"\d{3}"})
    assert ev2(ctx).value == 1.0
    # No pattern → 0 with comment.
    assert ev2(EvalContext(input="x", output="anything")).value == 0.0


def test_json_schema_match_evaluator() -> None:
    schema = {"type": "object", "required": ["answer"]}
    ev = JSONSchemaMatch(schema=schema)
    assert ev(EvalContext(input="q", output='{"answer": "Paris"}')).value == 1.0
    assert ev(EvalContext(input="q", output='{"wrong": "field"}')).value == 0.0
    # Not valid JSON.
    assert ev(EvalContext(input="q", output="not json")).value == 0.0
    # Wrong type.
    assert ev(
        EvalContext(input="q", output="[]")
    ).value == 0.0


def test_levenshtein_ratio_evaluator() -> None:
    ev = LevenshteinRatio()
    assert ev(EvalContext(input="x", output="Paris", expected_output="Paris")).value == 1.0
    score = ev(EvalContext(input="x", output="Paris", expected_output="Pari"))
    assert score.value is not None and 0.5 < score.value < 1.0


def test_latency_p95_evaluator() -> None:
    ev = LatencyP95(target_ms=500.0)
    fast = EvalContext(input="x", output="y", metadata={"latencies_ms": [100, 200, 300, 400]})
    assert ev(fast).value == 1.0
    slow = EvalContext(input="x", output="y", metadata={"latencies_ms": [1500, 1600, 1700]})
    assert ev(slow).value == 0.0
    # Single duration_ms also accepted.
    single = EvalContext(input="x", output="y", metadata={"duration_ms": 250.0})
    assert ev(single).value == 1.0


def test_cost_per_call_evaluator() -> None:
    ev = CostPerCall(budget_usd=0.10)
    cheap = EvalContext(input="x", output="y", metadata={"cost_usd": 0.02})
    score = ev(cheap)
    assert score.value is not None and 0.7 < score.value < 0.9
    over = EvalContext(input="x", output="y", metadata={"cost_usd": 0.15})
    assert ev(over).value == 0.0


def test_tool_use_correct_evaluator() -> None:
    ev = ToolUseCorrect()
    ctx = EvalContext(
        input="x",
        output={"tool_calls": ["search", "fetch", "summarise"]},
        expected_output={"tool_calls": ["search", "fetch", "summarise"]},
    )
    assert ev(ctx).value == 1.0
    # Partial overlap on a subsequence: LCS is 2 of max(3,4) = 0.5
    partial = EvalContext(
        input="x",
        output={"tool_calls": ["search", "browse", "summarise"]},
        expected_output={"tool_calls": ["search", "fetch", "summarise", "post"]},
    )
    score = partial
    val = ev(score).value
    assert val is not None and 0.4 < val < 0.6


def test_llm_judge_evaluator_uses_injected_judge() -> None:
    """A custom Judge can be injected so the evaluator doesn't hit the network."""

    class StubJudge:
        model = "stub-judge"

        def attribute(self, _obs, _cands):
            return [JudgeHypothesis(observation_id="x", confidence=0.85, reason="good")]

    ev = LLMJudgeEvaluator(rubric="Is the answer concise?", judge=StubJudge())
    score = ev(EvalContext(input="x", output="Paris", expected_output="Paris"))
    assert score.source == "LLM_JUDGE"
    assert score.value == 0.85
    assert score.judge_model == "stub-judge"
    assert "good" in (score.comment or "")


def test_llm_judge_evaluator_no_rubric_returns_zero() -> None:
    ev = LLMJudgeEvaluator(judge=DeterministicJudge())
    score = ev(EvalContext(input="x", output="y"))
    assert score.value == 0.0


def test_resolve_unknown_evaluator_raises() -> None:
    with pytest.raises(KeyError):
        resolve("nope_no_evaluator")


def test_registry_lists_all_builtins() -> None:
    names = set(registry().keys())
    expected = {
        "exact_match",
        "regex_match",
        "json_schema_match",
        "levenshtein_ratio",
        "llm_judge",
        "latency_p95",
        "cost_per_call",
        "tool_use_correct",
    }
    assert expected <= names


def test_register_custom_evaluator() -> None:
    """User-defined evaluators register and resolve."""

    class CustomEval:
        name = "custom_exact"
        version = "v1"

        def __call__(self, ctx: EvalContext) -> Score:
            return Score(
                project_id="",
                trace_id="",
                observation_id=None,
                name=self.name,
                value=1.0,
                source="HEURISTIC",
                comment="custom",
                timestamp=_NOW,
            )

    register("custom_exact", CustomEval)
    resolved = resolve("custom_exact")
    assert resolved.name == "custom_exact"
    assert resolved(EvalContext(input="x", output="y")).value == 1.0


# --------------------------------------------------------------------------- #
# Gate parser
# --------------------------------------------------------------------------- #


def test_parse_gate_supports_all_operators() -> None:
    for op in (">=", "<=", "==", "!=", ">", "<"):
        gate = parse_gate(f"acc{op}0.5")
        assert gate.op == op
        assert gate.threshold == 0.5


def test_parse_gate_rejects_invalid_expression() -> None:
    for bad in ("nope", "metric=>0.5", "no_value>=", ">=0.5"):
        with pytest.raises(ValueError, match="invalid gate"):
            parse_gate(bad)


def test_evaluate_gate_handles_missing_metric() -> None:
    gate = parse_gate("exact_match>=0.9")
    passed, reason = evaluate_gate(gate, {"some_other": 1.0})
    assert passed is False
    assert "missing metric" in reason


# --------------------------------------------------------------------------- #
# Offline runner — the SPEC §8.5 lines
# --------------------------------------------------------------------------- #


def test_eval_run_offline_writes_scores(
    backend: DuckDBBackend, control: ControlPlane, seeded: tuple[str, str]
) -> None:
    """§8.5 line 1: an offline run writes Score rows the backend can return."""
    project_id, dataset_id = seeded
    result = run_offline(
        backend=backend,
        control=control,
        project_id=project_id,
        dataset_id=dataset_id,
        evaluators=["exact_match", "levenshtein_ratio"],
        runtime=lambda ctx: ctx.expected_output,  # perfect-runtime echo
    )
    assert result.eval_run.status == "done"
    assert result.eval_run.sample_count == 2
    # 2 evaluators × 2 items = 4 scores persisted.
    assert len(result.scores) == 4
    # Mean exact_match for a perfect runtime should be 1.0.
    assert result.eval_run.summary["exact_match"] == 1.0
    # Scores carry the eval_run_id so the cockpit Evals tab can group them.
    assert all(s.eval_run_id == result.eval_run.id for s in result.scores)
    # And they're actually in the backend (per-trace lookup).
    sample = result.scores[0]
    persisted = backend.get_scores(project_id, sample.trace_id)
    assert any(s.name == "exact_match" for s in persisted)


def test_eval_run_gate_exits_nonzero(
    backend: DuckDBBackend, control: ControlPlane, seeded: tuple[str, str]
) -> None:
    """§8.5 line 2: a failing gate produces gate_passed=False on the result."""
    project_id, dataset_id = seeded
    # Use a runtime that always returns the wrong answer → exact_match = 0.0.
    result = run_offline(
        backend=backend,
        control=control,
        project_id=project_id,
        dataset_id=dataset_id,
        evaluators=["exact_match"],
        runtime=lambda _ctx: "wrong-answer",
        gate=parse_gate("exact_match>=0.9"),
    )
    assert result.eval_run.gate_passed is False
    assert result.eval_run.status == "failed"


def test_eval_run_offline_default_echo_runtime(
    backend: DuckDBBackend, control: ControlPlane, seeded: tuple[str, str]
) -> None:
    """No runtime → echo (output = input)."""
    project_id, dataset_id = seeded
    result = run_offline(
        backend=backend,
        control=control,
        project_id=project_id,
        dataset_id=dataset_id,
        evaluators=["levenshtein_ratio"],
    )
    # Echo means output != expected_output, so ratio is low.
    assert result.eval_run.summary["levenshtein_ratio"] < 0.8


def test_eval_run_offline_handles_runtime_exception(
    backend: DuckDBBackend, control: ControlPlane, seeded: tuple[str, str]
) -> None:
    """A raising runtime must not crash the run; evaluators see output=None."""
    project_id, dataset_id = seeded

    def boom(_ctx):
        raise RuntimeError("runtime exploded")

    result = run_offline(
        backend=backend,
        control=control,
        project_id=project_id,
        dataset_id=dataset_id,
        evaluators=["exact_match"],
        runtime=boom,
    )
    assert result.eval_run.sample_count == 2
    # exact_match against None output is 0.
    assert result.eval_run.summary["exact_match"] == 0.0


def test_eval_run_offline_rejects_empty_evaluator_list(
    backend: DuckDBBackend, control: ControlPlane, seeded: tuple[str, str]
) -> None:
    project_id, dataset_id = seeded
    with pytest.raises(ValueError, match="at least one evaluator"):
        run_offline(
            backend=backend,
            control=control,
            project_id=project_id,
            dataset_id=dataset_id,
            evaluators=[],
        )


# --------------------------------------------------------------------------- #
# Online sampler
# --------------------------------------------------------------------------- #


def test_should_sample_online_zero_pct_never_samples() -> None:
    assert should_sample_online(trace_id="abc123def456", sample_pct=0) is False


def test_should_sample_online_full_pct_always_samples() -> None:
    assert should_sample_online(trace_id="abc123def456", sample_pct=100) is True


def test_should_sample_online_deterministic() -> None:
    """Same trace_id + sample_pct → same answer, every call."""
    tid = "deadbeefcafef00d"
    a = should_sample_online(trace_id=tid, sample_pct=37)
    b = should_sample_online(trace_id=tid, sample_pct=37)
    assert a == b


def test_should_sample_online_rough_rate() -> None:
    """Across 1000 random hex trace_ids, sampling rate is within ±10%."""
    import random

    rng = random.Random(1337)
    sampled = 0
    for _ in range(1000):
        tid = "".join(rng.choice("0123456789abcdef") for _ in range(16))
        if should_sample_online(trace_id=tid, sample_pct=20):
            sampled += 1
    # 20% nominal ± 10% slack.
    assert 100 <= sampled <= 300


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #


def _score(
    name: str, source: str, value: float, *, trace_id: str = "t1", judge_model: str = "judge-1"
) -> Score:
    return Score(
        project_id="p1",
        trace_id=trace_id,
        observation_id=None,
        name=name,
        value=value,
        source=source,
        comment=None,
        judge_model=judge_model if source == "LLM_JUDGE" else None,
        timestamp=_NOW,
    )


def test_calibration_pairs_and_bias() -> None:
    judge_scores = [
        _score("quality", "LLM_JUDGE", 0.9, trace_id="t1"),
        _score("quality", "LLM_JUDGE", 0.7, trace_id="t2"),
    ]
    human_scores = [
        _score("quality", "ANNOTATION", 0.7, trace_id="t1"),
        _score("quality", "ANNOTATION", 0.6, trace_id="t2"),
    ]
    pairs = pair_scores(judge_scores, human_scores)
    assert len(pairs) == 2
    bias = compute_bias(judge_scores, human_scores)
    report = bias[("quality", "judge-1")]
    assert report.samples == 2
    # Mean of (0.9-0.7, 0.7-0.6) = 0.15
    assert abs(report.bias - 0.15) < 1e-9


def test_calibration_apply_bias_clamps() -> None:
    s = _score("quality", "LLM_JUDGE", 0.5)
    shifted = apply_bias(s, bias=-2.0)  # would push to 2.5, clamped to 1.0
    assert shifted.value == 1.0
    assert shifted.calibration_bias == -2.0


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #


def test_cli_eval_run_writes_scores_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hfao eval run` against a passing runtime returns exit-code 0."""
    duck = tmp_path / "hfao.duckdb"
    cp_path = tmp_path / "cp.db"
    monkeypatch.setenv("HFAO_DUCKDB_PATH", str(duck))
    monkeypatch.setenv("HFAO_CONTROL_PLANE_DSN", f"sqlite:///{cp_path}")
    monkeypatch.setenv("HFAO_PROJECT", "ac8")
    monkeypatch.setenv("HFAO_BACKEND", "duckdb")
    sys.modules.pop("hfao.cli", None)

    # Pre-seed dataset.
    cp = ControlPlane(f"sqlite:///{cp_path}")
    cp.init_schema()
    ws = cp.create_workspace(slug="default", name="Default")
    cp.create_project_with_id(
        project_id="ac8", workspace_id=ws["id"], slug="ac8", name="ac8"
    )
    ds = cp.create_dataset(project_id="ac8", name="goldens")
    cp.add_dataset_item(
        project_id="ac8", dataset_id=ds["id"], input="2+2", expected_output="4"
    )
    cp.close()

    # Import cli AFTER env is set, so its globals see the test config.
    from hfao.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["eval", "run", ds["id"], "-e", "exact_match"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stdout
    assert "exact_match" in result.stdout


def test_cli_eval_run_gate_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hfao eval run --gate exact_match>=0.9` fails when score is below threshold."""
    duck = tmp_path / "hfao.duckdb"
    cp_path = tmp_path / "cp.db"
    monkeypatch.setenv("HFAO_DUCKDB_PATH", str(duck))
    monkeypatch.setenv("HFAO_CONTROL_PLANE_DSN", f"sqlite:///{cp_path}")
    monkeypatch.setenv("HFAO_PROJECT", "ac8")
    monkeypatch.setenv("HFAO_BACKEND", "duckdb")
    sys.modules.pop("hfao.cli", None)

    cp = ControlPlane(f"sqlite:///{cp_path}")
    cp.init_schema()
    ws = cp.create_workspace(slug="default", name="Default")
    cp.create_project_with_id(
        project_id="ac8", workspace_id=ws["id"], slug="ac8", name="ac8"
    )
    ds = cp.create_dataset(project_id="ac8", name="goldens")
    # Dataset item whose echo-runtime output does NOT equal expected_output.
    cp.add_dataset_item(
        project_id="ac8",
        dataset_id=ds["id"],
        input="2+2",
        expected_output="answer-is-4-which-will-never-match-the-echo",
    )
    cp.close()

    from hfao.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "eval", "run", ds["id"],
            "-e", "exact_match",
            "--gate", "exact_match>=0.9",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "passed=False" in result.stdout
