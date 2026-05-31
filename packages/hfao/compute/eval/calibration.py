"""Judge ↔ human alignment for LLM-judge evaluators (SPEC §8.2).

Tracks how far an ``LLM_JUDGE`` evaluator's scores drift from human
``ANNOTATION`` scores on the *same* (trace, observation, name) tuple. The
:func:`compute_bias` helper returns a ``Score.calibration_bias`` value the
runner persists onto judge-emitted scores so downstream consumers (cockpit
Evals tab, MCP get_trace) can show the calibrated value alongside the raw
one.

Bias is a simple shift, not a regression model: judge_value - human_value
averaged over recent paired samples. The cockpit / MCP read side surfaces
both the raw and calibrated number — never replaces the raw.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from hfao.schema.scores import Score


@dataclass(frozen=True)
class CalibrationReport:
    """Per-(name, judge_model) calibration summary."""

    name: str
    judge_model: str
    samples: int
    bias: float
    rmse: float


def pair_scores(
    judge_scores: Iterable[Score], human_scores: Iterable[Score]
) -> list[tuple[Score, Score]]:
    """Pair judge + human scores on (trace_id, observation_id, name)."""
    by_key: dict[tuple[str, str, str], Score] = {}
    for s in human_scores:
        if s.source != "ANNOTATION":
            continue
        key = (s.trace_id, s.observation_id or "", s.name)
        by_key[key] = s
    pairs: list[tuple[Score, Score]] = []
    for j in judge_scores:
        if j.source != "LLM_JUDGE":
            continue
        key = (j.trace_id, j.observation_id or "", j.name)
        if key in by_key:
            pairs.append((j, by_key[key]))
    return pairs


def compute_bias(
    judge_scores: Iterable[Score], human_scores: Iterable[Score]
) -> dict[tuple[str, str], CalibrationReport]:
    """Aggregate bias per ``(score.name, judge_model)``."""
    by_group: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for judged, human in pair_scores(judge_scores, human_scores):
        if judged.value is None or human.value is None:
            continue
        key = (judged.name, judged.judge_model or "")
        by_group.setdefault(key, []).append((judged.value, human.value))
    out: dict[tuple[str, str], CalibrationReport] = {}
    for (name, model), samples in by_group.items():
        if not samples:
            continue
        diffs = [j - h for j, h in samples]
        mean_diff = sum(diffs) / len(diffs)
        rmse = (sum((d - mean_diff) ** 2 for d in diffs) / len(diffs)) ** 0.5
        out[(name, model)] = CalibrationReport(
            name=name,
            judge_model=model,
            samples=len(samples),
            bias=mean_diff,
            rmse=rmse,
        )
    return out


def apply_bias(score: Score, bias: float) -> Score:
    """Return a copy of ``score`` with ``calibration_bias`` set and ``value`` shifted.

    The shifted value is clamped to [0.0, 1.0]; the raw value is not modified
    (callers must persist *both* the original and the calibrated copy if they
    want the raw available — the runner persists only the calibrated copy in
    v1 since `Score.calibration_bias` survives the round trip and the raw can
    be recovered as `value + calibration_bias`).
    """
    if score.value is None:
        return score
    shifted = max(0.0, min(1.0, score.value - bias))
    return Score(
        project_id=score.project_id,
        trace_id=score.trace_id,
        observation_id=score.observation_id,
        name=score.name,
        value=shifted,
        string_value=score.string_value,
        source=score.source,
        comment=score.comment,
        judge_model=score.judge_model,
        calibration_bias=bias,
        timestamp=score.timestamp,
        annotator_id=score.annotator_id,
        eval_run_id=score.eval_run_id,
    )


__all__ = [
    "CalibrationReport",
    "apply_bias",
    "compute_bias",
    "pair_scores",
]
