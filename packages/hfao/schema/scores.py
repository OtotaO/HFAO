from datetime import datetime
from typing import Literal

from msgspec import Struct


class Score(Struct, kw_only=True):
    project_id: str
    trace_id: str
    observation_id: str | None = None
    name: str
    value: float | None = None
    string_value: str | None = None
    source: Literal["ANNOTATION", "LLM_JUDGE", "HEURISTIC", "EXTERNAL"]
    comment: str | None = None
    judge_model: str | None = None
    calibration_bias: float = 0.0
    timestamp: datetime
    annotator_id: str | None = None
    eval_run_id: str | None = None
