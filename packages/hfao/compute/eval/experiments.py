"""Experiment runner (SPEC §8.2 + §16 Q-10a).

Multi-variant tournament loop on top of the single-variant eval engine
(:mod:`hfao.compute.eval.runner`). For each dataset item × variant × seed
the runner:

  1. resolves the variant-aware ``Runtime`` (per-variant config substitution),
  2. invokes it inside an :class:`EvalContext`,
  3. applies every evaluator from the definition,
  4. persists Score rows (with ``eval_run_id``) and an
     :class:`ExperimentRun` join,
  5. records a :class:`Pairing` when the same seed lands across every
     variant for the same dataset item.

After all runs complete, :func:`compute_verdicts` produces one
:class:`Verdict` per evaluator: ranking, per-variant means, percentile
bootstrap 95% CIs, and a paired-test p-value (Wilcoxon signed-rank by
default; configurable per call).

The experiment runner **does not** mutate the
:class:`ExperimentDefinition`. Q-10a.3 promised immutable definitions —
the runner's contract is one definition → many Verdicts, append-only.

A thin :func:`verdict_matrix` aggregation helper covers the
matrix-view use case promised in Q-10a.2 without breaking the
one-Verdict-per-evaluator audit shape.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from hfao.compute.eval.engine import EvalContext, resolve
from hfao.compute.eval.runner import maybe_json
from hfao.schema.experiments import (
    PairedTest,
    Variant,
    Verdict,
)
from hfao.schema.scores import Score

if TYPE_CHECKING:
    from hfao.storage import StorageBackend
    from hfao.storage.control_plane import ControlPlane


VariantRuntime = Callable[[Variant, EvalContext], Any]
"""User-supplied runtime: given a Variant + an EvalContext (input already set,
output empty), return the variant's output. Stays free of network requirements
so AC tests can inject pure-Python tournaments."""


@dataclass(frozen=True)
class ExperimentRunResult:
    """Return shape of :func:`run_experiment`."""

    experiment_id: str
    run_count: int
    pairing_count: int
    verdicts: list[Verdict]


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run_experiment(
    *,
    backend: StorageBackend,
    control: ControlPlane,
    project_id: str,
    experiment_id: str,
    runtime: VariantRuntime,
    bootstrap_iterations: int = 2000,
    paired_test: PairedTest = "wilcoxon_signed_rank",
    rng_seed: int = 1337,
) -> ExperimentRunResult:
    """Run every variant against every dataset item ``planned_runs_per_variant``
    times; persist scores + ExperimentRun joins + Pairings + Verdicts.

    The runner uses a deterministic seed sequence: ``(rng_seed +
    dataset_item_index * 1009 + run_index)``. Same ``rng_seed`` across
    variants is the *whole point* — it's what makes pairings paired.

    ``runtime`` is the user-supplied function that maps ``(Variant, EvalContext)``
    to the variant's output. Tests inject a deterministic stub; production
    wires this to an HTTP runtime or in-process model invocation.
    """
    experiment = control.get_experiment(
        project_id=project_id, experiment_id=experiment_id
    )
    definition = control.get_experiment_definition(
        project_id=project_id, def_id=str(experiment["definition_id"])
    )
    raw_variants = cast("list[dict[str, Any]]", json.loads(definition["variants"]))
    variants = [
        Variant(
            id=str(v["id"]),
            name=str(v["name"]),
            axis=v["axis"],
            config_hash=str(v["config_hash"]),
            config={
                str(k): str(val)
                for k, val in cast("dict[str, Any]", v.get("config") or {}).items()
            },
        )
        for v in raw_variants
    ]
    if not variants:
        raise ValueError(f"experiment {experiment_id} has no variants")

    evaluator_ids: list[str] = list(json.loads(definition["evaluator_ids"]))
    if not evaluator_ids:
        raise ValueError(f"experiment {experiment_id} has no evaluators")

    items = control.list_dataset_items(
        project_id=project_id, dataset_id=str(definition["dataset_id"])
    )
    if not items:
        raise ValueError(
            f"dataset {definition['dataset_id']!r} has no items; nothing to run"
        )

    started = datetime.now(timezone.utc)
    control.set_experiment_status(
        project_id=project_id,
        experiment_id=experiment_id,
        status="running",
        started_at=started.isoformat(),
    )

    planned = int(definition["planned_runs_per_variant"])
    resolved_evaluators = {name: resolve(name) for name in evaluator_ids}

    # Per-evaluator: {variant_id: [score_value, ...]} aligned across variants
    # by pairing position so paired statistics see matched samples.
    paired_samples: dict[str, dict[str, list[float]]] = {
        ev: {v.id: [] for v in variants} for ev in evaluator_ids
    }

    run_count = 0
    pairing_count = 0

    for item_index, item in enumerate(items):
        for run_index in range(planned):
            seed = rng_seed + item_index * 1009 + run_index
            ctx_input = maybe_json(item["input"])
            ctx_expected = maybe_json(item.get("expected_output"))
            ctx_metadata = _safe_dict(item.get("metadata") or "{}") | {
                "dataset_item_id": item["id"],
                "seed": seed,
            }
            per_variant_scores: dict[str, dict[str, float]] = {}
            run_ids_by_variant: dict[str, str] = {}

            for variant in variants:
                trace_id = (
                    f"exp-{experiment_id}-{item['id']}-{variant.id}-{run_index}"
                )
                run_ctx = EvalContext(
                    input=ctx_input,
                    output=None,
                    expected_output=ctx_expected,
                    metadata=ctx_metadata | {"variant_id": variant.id, "trace_id": trace_id},
                )
                try:
                    output = runtime(variant, run_ctx)
                except Exception as exc:  # noqa: BLE001 — capture per-run errors
                    output = None
                    run_ctx = EvalContext(
                        input=run_ctx.input,
                        output=None,
                        expected_output=run_ctx.expected_output,
                        metadata={**run_ctx.metadata, "runtime_error": str(exc)},
                    )
                run_ctx = EvalContext(
                    input=run_ctx.input,
                    output=output,
                    expected_output=run_ctx.expected_output,
                    metadata=run_ctx.metadata,
                )

                scores_for_run: list[Score] = []
                eval_run_id = f"er_{uuid.uuid4().hex[:24]}"
                for name, ev in resolved_evaluators.items():
                    raw = ev(run_ctx)
                    persisted = Score(
                        project_id=project_id,
                        trace_id=trace_id,
                        observation_id=None,
                        name=raw.name,
                        value=raw.value,
                        string_value=raw.string_value,
                        source=raw.source,
                        comment=raw.comment,
                        judge_model=raw.judge_model,
                        calibration_bias=raw.calibration_bias,
                        timestamp=raw.timestamp,
                        annotator_id=raw.annotator_id,
                        eval_run_id=eval_run_id,
                    )
                    scores_for_run.append(persisted)
                    per_variant_scores.setdefault(variant.id, {})[name] = (
                        float(raw.value) if raw.value is not None else 0.0
                    )
                backend.write_scores(scores_for_run)

                control.record_experiment_run(
                    project_id=project_id,
                    experiment_id=experiment_id,
                    variant_id=variant.id,
                    trace_id=trace_id,
                    seed=seed,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
                run_ids_by_variant[variant.id] = trace_id
                run_count += 1

            # Record a Pairing iff every variant produced a run for this seed
            # and item — the schema invariant Q-10a.2 protects against.
            if set(run_ids_by_variant.keys()) == {v.id for v in variants}:
                control.record_pairing(
                    experiment_id=experiment_id,
                    dataset_item_id=item["id"],
                    seed=seed,
                    run_ids_by_variant=run_ids_by_variant,
                )
                pairing_count += 1
                for ev_name in evaluator_ids:
                    for variant in variants:
                        paired_samples[ev_name][variant.id].append(
                            per_variant_scores.get(variant.id, {}).get(ev_name, 0.0)
                        )

    verdicts = compute_verdicts(
        experiment_id=experiment_id,
        variants=variants,
        evaluator_ids=evaluator_ids,
        paired_samples=paired_samples,
        bootstrap_iterations=bootstrap_iterations,
        paired_test=paired_test,
        rng_seed=rng_seed,
    )
    for v in verdicts:
        control.record_verdict(
            experiment_id=v.experiment_id,
            evaluator=v.evaluator,
            ranking=v.ranking,
            mean_by_variant=v.mean_by_variant,
            ci_low_by_variant=v.ci_low_by_variant,
            ci_high_by_variant=v.ci_high_by_variant,
            n_pairings=v.n_pairings,
            paired_test=v.paired_test,
            p_value=v.p_value,
        )

    finished = datetime.now(timezone.utc)
    control.set_experiment_status(
        project_id=project_id,
        experiment_id=experiment_id,
        status="complete",
        finished_at=finished.isoformat(),
    )

    return ExperimentRunResult(
        experiment_id=experiment_id,
        run_count=run_count,
        pairing_count=pairing_count,
        verdicts=verdicts,
    )


# --------------------------------------------------------------------------- #
# Verdict computation
# --------------------------------------------------------------------------- #


def compute_verdicts(
    *,
    experiment_id: str,
    variants: list[Variant],
    evaluator_ids: list[str],
    paired_samples: dict[str, dict[str, list[float]]],
    bootstrap_iterations: int = 2000,
    paired_test: PairedTest = "wilcoxon_signed_rank",
    rng_seed: int = 0,
) -> list[Verdict]:
    """One :class:`Verdict` per evaluator (Q-10a.2 → Option A)."""
    rng = random.Random(rng_seed)
    out: list[Verdict] = []
    for ev_name in evaluator_ids:
        samples = paired_samples.get(ev_name, {})
        means: dict[str, float] = {}
        ci_low: dict[str, float] = {}
        ci_high: dict[str, float] = {}
        n_pairs = min(
            (len(samples.get(v.id, [])) for v in variants), default=0
        )
        for v in variants:
            values = samples.get(v.id, [])
            means[v.id] = statistics.fmean(values) if values else 0.0
            low, high = _bootstrap_ci(values, bootstrap_iterations, rng)
            ci_low[v.id] = low
            ci_high[v.id] = high
        ranking = sorted(
            (v.id for v in variants),
            key=lambda vid: means[vid],
            reverse=True,
        )
        p_value: float | None = None
        if (
            paired_test != "none"
            and len(variants) == 2
            and n_pairs >= 2
        ):
            a = samples[variants[0].id]
            b = samples[variants[1].id]
            p_value = _paired_test(a, b, paired_test)

        out.append(
            Verdict(
                experiment_id=experiment_id,
                evaluator=ev_name,
                ranking=ranking,
                mean_by_variant=means,
                ci_low_by_variant=ci_low,
                ci_high_by_variant=ci_high,
                n_pairings=n_pairs,
                paired_test=paired_test if len(variants) == 2 else "none",
                p_value=p_value,
                computed_at=datetime.now(timezone.utc),
            )
        )
    return out


def verdict_matrix(
    verdicts: list[Verdict],
) -> dict[str, dict[str, float]]:
    """Q-10a.2 helper: ``{evaluator: {variant_id: mean}}`` from a Verdict list.

    Pure aggregation — does not write to the control plane. The matrix view
    is the natural cockpit / MCP render shape; the Verdict log itself stays
    append-only.
    """
    matrix: dict[str, dict[str, float]] = {}
    for v in verdicts:
        matrix[v.evaluator] = dict(v.mean_by_variant)
    return matrix


# --------------------------------------------------------------------------- #
# Statistics — implemented inline to avoid pulling scipy as a top-level dep.
# --------------------------------------------------------------------------- #


def _bootstrap_ci(
    values: list[float],
    iterations: int,
    rng: random.Random,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean. Returns (low, high)."""
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return values[0], values[0]
    means: list[float] = []
    n = len(values)
    for _ in range(iterations):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(resample))
    means.sort()
    alpha = 1.0 - confidence
    low_idx = max(0, int(alpha / 2 * iterations))
    high_idx = min(iterations - 1, int((1 - alpha / 2) * iterations))
    return means[low_idx], means[high_idx]


def _paired_test(a: list[float], b: list[float], test: PairedTest) -> float | None:
    """Return a p-value for the paired comparison of ``a`` vs ``b``."""
    if len(a) != len(b) or len(a) < 2:
        return None
    if test == "wilcoxon_signed_rank":
        return _wilcoxon_signed_rank(a, b)
    if test == "paired_t":
        return _paired_t(a, b)
    if test == "sign_test":
        return _sign_test(a, b)
    return None


def _wilcoxon_signed_rank(a: list[float], b: list[float]) -> float | None:
    """Two-sided p-value via the normal approximation. Drops zero-difference
    pairs (the standard Wilcoxon convention)."""
    diffs = [x - y for x, y in zip(a, b, strict=True) if x != y]
    n = len(diffs)
    if n < 2:
        return None
    abs_diffs = sorted(((abs(d), d) for d in diffs), key=lambda t: t[0])
    # Average-rank tie handling.
    ranks: list[float] = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_diffs[j + 1][0] == abs_diffs[i][0]:
            j += 1
        avg = (i + j + 2) / 2  # 1-based average rank
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    w_plus = sum(r for r, (_, d) in zip(ranks, abs_diffs, strict=True) if d > 0)
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24
    if var <= 0:
        return None
    z = (w_plus - mean) / math.sqrt(var)
    # Two-sided p via normal CDF.
    p = 2 * (1 - _phi(abs(z)))
    return max(0.0, min(1.0, p))


def _paired_t(a: list[float], b: list[float]) -> float | None:
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    n = len(diffs)
    if n < 2:
        return None
    mean = statistics.fmean(diffs)
    sd = statistics.pstdev(diffs)
    if sd == 0:
        return 0.0 if mean != 0 else 1.0
    t = mean / (sd / math.sqrt(n))
    # Two-sided p via the normal approximation (n is usually small but this
    # avoids a scipy dependency; users wanting exact t can post-process the
    # raw samples).
    p = 2 * (1 - _phi(abs(t)))
    return max(0.0, min(1.0, p))


def _sign_test(a: list[float], b: list[float]) -> float | None:
    diffs = [x - y for x, y in zip(a, b, strict=True) if x != y]
    n = len(diffs)
    if n < 2:
        return None
    positives = sum(1 for d in diffs if d > 0)
    # Two-sided binomial p.
    p_one = sum(_binom(n, k) for k in range(positives, n + 1)) / (2**n)
    return min(1.0, 2 * p_one)


def _binom(n: int, k: int) -> int:
    return math.comb(n, k)


def _phi(z: float) -> float:
    """Standard-normal CDF via erfc."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _safe_dict(raw: Any) -> dict[str, Any]:
    parsed = maybe_json(raw)
    if isinstance(parsed, dict):
        return {str(k): v for k, v in parsed.items()}  # type: ignore[misc]
    return {}


__all__ = [
    "ExperimentRunResult",
    "VariantRuntime",
    "compute_verdicts",
    "run_experiment",
    "verdict_matrix",
]
