"""Eval engine (SPEC §8.2).

Public surface:

  * :class:`EvalContext` / :class:`Evaluator` / :func:`resolve` — engine.
  * Built-in evaluators (8) registered at import time.
  * :func:`run_offline` / :func:`run_eval` / :func:`should_sample_online`
    — single-variant runner.
  * :func:`run_experiment` / :func:`compute_verdicts` / :func:`verdict_matrix`
    — multi-variant tournament runner (§16 Q-10a).
  * :func:`parse_gate` / :func:`evaluate_gate` — CI gate parser.
  * :func:`compute_bias` / :func:`apply_bias` — calibration.
"""

# Force-load built-ins so their factories are registered.
from hfao.compute.eval import builtin  # noqa: E402, F401
from hfao.compute.eval.calibration import (
    CalibrationReport,
    apply_bias,
    compute_bias,
    pair_scores,
)
from hfao.compute.eval.engine import (
    EvalContext,
    EvalSpec,
    Evaluator,
    register,
    registry,
    resolve,
)
from hfao.compute.eval.experiments import (
    ExperimentRunResult,
    VariantRuntime,
    compute_verdicts,
    run_experiment,
    verdict_matrix,
)
from hfao.compute.eval.runner import (
    Gate,
    OfflineResult,
    Runtime,
    echo_runtime,
    evaluate_gate,
    http_runtime,
    parse_gate,
    run_eval,
    run_offline,
    should_sample_online,
)

__all__ = [
    "CalibrationReport",
    "EvalContext",
    "EvalSpec",
    "Evaluator",
    "ExperimentRunResult",
    "Gate",
    "OfflineResult",
    "Runtime",
    "VariantRuntime",
    "apply_bias",
    "builtin",
    "compute_bias",
    "compute_verdicts",
    "echo_runtime",
    "evaluate_gate",
    "http_runtime",
    "pair_scores",
    "parse_gate",
    "register",
    "registry",
    "resolve",
    "run_eval",
    "run_experiment",
    "run_offline",
    "should_sample_online",
    "verdict_matrix",
]
