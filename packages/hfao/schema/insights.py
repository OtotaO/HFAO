"""Insight schema (SPEC §16 Q-18).

An :class:`Insight` is a *detected* observation about a project's signals —
distinct from an :class:`hfao.schema.monitors.Alert`, which is *configured*
(user-defined threshold breach). The Q-18 distinction matters: alerts
require the user to know what to watch in advance; insights surface patterns
the user didn't pre-define.

Insights are append-only. The anomaly engine never updates or deletes a row;
the cockpit and the MCP ``list_insights`` tool dedup by
``(project_id, signal_name, kind, observed_at::date)`` on render so a single
day's drift doesn't multiply across worker ticks.

Severities are linear and ordered: ``info < notice < warning < critical``.
The cockpit and the MCP ``list_insights(min_severity=...)`` argument both
honour the ordering for filtering.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from msgspec import Struct, field

InsightKind = Literal[
    # Detected anomalies (the Q-18 net-new surface).
    "trend_shift",
    "threshold_breach_implicit",
    "distribution_drift",
    "calibration_drift",
    "cost_anomaly",
    # Lifted from Stage-2 COUNTERFACTUAL_REPLAY edges per Q-20 / Q-18 follow-up:
    # every replay-verified decisive error surfaces as an Insight too.
    "decisive_error_verified_by_replay",
]

InsightSeverity = Literal["info", "notice", "warning", "critical"]


SEVERITY_RANK: dict[InsightSeverity, int] = {
    "info": 0,
    "notice": 1,
    "warning": 2,
    "critical": 3,
}


class Insight(Struct, kw_only=True):
    project_id: str
    id: str
    kind: InsightKind
    severity: InsightSeverity
    signal_name: str             # e.g. "error_rate", "total_cost_usd", "judge.quality"
    baseline_value: float
    current_value: float
    observed_at: datetime
    evidence_sql: str            # the read-only SQL the detector ran to produce this
    confidence: float            # 0.0–1.0; engine-specific (Z-score → CDF, KS → 1-p, …)
    summary: str = ""            # short human-readable one-liner for the cockpit list
    trace_id: str | None = None  # set only for `decisive_error_verified_by_replay`
    metadata: dict[str, str] = field(default_factory=lambda: {})


def severity_ge(a: InsightSeverity, b: InsightSeverity) -> bool:
    """``a`` is at least as severe as ``b`` per the ``SEVERITY_RANK`` order."""
    return SEVERITY_RANK[a] >= SEVERITY_RANK[b]


__all__ = [
    "SEVERITY_RANK",
    "Insight",
    "InsightKind",
    "InsightSeverity",
    "severity_ge",
]
