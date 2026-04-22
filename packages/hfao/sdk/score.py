"""HFAO SDK ``score()`` surface.

SPEC §4.1 / §5.1. ``hfao.score()`` attaches a :class:`Score` to the current
observation by emitting a ``gen_ai.evaluation.result`` span event on the
active span. The ingest normalizer (:func:`hfao.ingest.normalize.normalize_scores`)
extracts those events into canonical ``Score`` rows on arrival — keeping
the SDK wire-compatible with OTel GenAI without a bespoke HFAO endpoint.

For scoring a span after it has closed (e.g. asynchronous LLM-judge
replies), callers can pass ``observation_id`` and ``trace_id`` to bind
the score to a specific observation without needing the span to still
be active.
"""

from __future__ import annotations

from typing import Literal

from opentelemetry import trace

ScoreSource = Literal["ANNOTATION", "LLM_JUDGE", "HEURISTIC", "EXTERNAL"]


def score(
    name: str,
    *,
    value: float | None = None,
    string_value: str | None = None,
    source: ScoreSource = "EXTERNAL",
    comment: str | None = None,
    judge_model: str | None = None,
    observation_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Attach a :class:`Score` to the current (or addressed) observation.

    Emits a ``gen_ai.evaluation.result`` span event on the active span
    with the OTel GenAI attribute names. If ``observation_id`` is given,
    the event carries ``hfao.observation_id`` so a later post-mortem
    normaliser can bind the score to that observation even when the
    active span has already ended.

    At least one of ``value`` / ``string_value`` must be provided; both
    are permitted (``value`` is the numeric ranking key, ``string_value``
    is the human label).
    """
    if value is None and string_value is None:
        raise ValueError("score() requires value or string_value (or both)")

    span = trace.get_current_span()
    attributes: dict[str, str | float | int | bool] = {
        "gen_ai.evaluation.name": name,
        "hfao.score.source": source,
    }
    if value is not None:
        attributes["gen_ai.evaluation.score.value"] = float(value)
    if string_value is not None:
        attributes["gen_ai.evaluation.score.label"] = string_value
    if comment is not None:
        attributes["gen_ai.evaluation.explanation"] = comment
    if judge_model is not None:
        attributes["hfao.score.judge_model"] = judge_model
    if observation_id is not None:
        attributes["hfao.observation_id"] = observation_id
    if trace_id is not None:
        attributes["hfao.trace_id"] = trace_id

    # The span may be a non-recording sentinel when called outside any
    # hfao.init() context; in that case we silently drop the score
    # rather than raising — matches OpenInference / OTel conventions.
    if span.is_recording():
        span.add_event(name="gen_ai.evaluation.result", attributes=attributes)


__all__ = ["score", "ScoreSource"]
