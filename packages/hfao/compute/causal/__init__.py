"""Causal-attribution pipeline (SPEC §8.1).

  * :mod:`hfao.compute.causal.static`         — Stage 1, lexical/structural.
  * :mod:`hfao.compute.causal.judge`          — Stage 3, LLM judge.
  * :mod:`hfao.compute.causal.counterfactual` — Stage 2 (Phase 2; empty in v1).
  * :mod:`hfao.compute.causal.pipeline`       — orchestrator + persistence.
  * :mod:`hfao.compute.causal.replay_support` — per-framework replay flag.
"""

from hfao.compute.causal.judge import (
    DeterministicJudge,
    Judge,
    JudgeHypothesis,
    select_judge,
    stage3_judge,
)
from hfao.compute.causal.pipeline import (
    AttributionResult,
    attribute_failure,
    should_attribute,
)
from hfao.compute.causal.replay_support import (
    REPLAY_SUPPORTED_FRAMEWORKS,
    REPLAY_UNSUPPORTED_FRAMEWORKS,
    framework_of,
    is_replay_supported,
    replay_supported_for_observations,
)
from hfao.compute.causal.static import MIN_LEN, stage1_static

__all__ = [
    "MIN_LEN",
    "REPLAY_SUPPORTED_FRAMEWORKS",
    "REPLAY_UNSUPPORTED_FRAMEWORKS",
    "AttributionResult",
    "DeterministicJudge",
    "Judge",
    "JudgeHypothesis",
    "attribute_failure",
    "framework_of",
    "is_replay_supported",
    "replay_supported_for_observations",
    "select_judge",
    "should_attribute",
    "stage1_static",
    "stage3_judge",
]
