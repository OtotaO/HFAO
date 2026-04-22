"""Hugging Face Agent Observatory.

SPEC §12.1. The top-level namespace re-exports the SDK surface so that
``import hfao; hfao.init(...)`` works as the canonical entry point.
"""

from __future__ import annotations

from hfao.sdk import (
    HFAOContext,
    current_context,
    current_session,
    init,
    observe,
    prompt,
    require_context,
    reset_for_testing,
    score,
    session,
)

__all__ = [
    "HFAOContext",
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
