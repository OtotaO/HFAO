"""Eval run schema (SPEC §8.2).

A persisted record of one offline-eval invocation against a dataset.
Online-eval triggers (``on_trace_close`` + ``where_sql``) reuse this same
shape but are emitted via :mod:`hfao.compute.eval.runner` rather than a
dedicated record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from msgspec import Struct, field

EvalRunStatus = Literal["pending", "running", "done", "failed"]


class EvalRun(Struct, kw_only=True):
    id: str
    project_id: str
    dataset_id: str
    evaluators: list[str]
    status: EvalRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    summary: dict[str, float] = field(default_factory=lambda: {})
    gate_expression: str | None = None
    gate_passed: bool | None = None
    runtime_url: str | None = None
    sample_count: int = 0
    failure_reason: str | None = None
