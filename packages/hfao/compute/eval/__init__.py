"""Eval engine (SPEC §8.2).

Public surface:

  * :class:`EvalContext` / :class:`Evaluator` / :func:`resolve` — engine.
  * Built-in evaluators (8) registered at import time.
  * :func:`run_offline` / :func:`run_eval` / :func:`should_sample_online`
    — runner.
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
    "Gate",
    "OfflineResult",
    "Runtime",
    "apply_bias",
    "builtin",
    "compute_bias",
    "echo_runtime",
    "evaluate_gate",
    "http_runtime",
    "pair_scores",
    "parse_gate",
    "register",
    "registry",
    "resolve",
    "run_eval",
    "run_offline",
    "should_sample_online",
]
