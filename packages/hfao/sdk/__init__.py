"""HFAO user-facing SDK.

SPEC §12.1. Re-exports the public surface of :mod:`hfao.sdk` so callers
can ``from hfao.sdk import init, session, score, observe, prompt`` and
get the same objects the top-level ``hfao`` namespace exposes.
"""

from __future__ import annotations

from hfao.sdk.context import (
    HFAOBaggageSpanProcessor,
    HFAOContext,
    current_session,
    prompt,
    session,
)
from hfao.sdk.decorators import ObserveKind, observe
from hfao.sdk.init import current_context, init, require_context, reset_for_testing
from hfao.sdk.score import ScoreSource, score

__all__ = [
    "HFAOBaggageSpanProcessor",
    "HFAOContext",
    "ObserveKind",
    "ScoreSource",
    "current_context",
    "current_session",
    "init",
    "observe",
    "prompt",
    "require_context",
    "reset_for_testing",
    "score",
    "session",
]
