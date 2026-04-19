from datetime import datetime
from typing import Literal

from msgspec import Struct

EdgeType = Literal[
    "DATAFLOW",
    "HANDOFF",
    "TOOL_DEPENDENCY",
    "PROMPT_CONDITIONING",
    "DECISIVE_ERROR",
]

Method = Literal["STATIC", "COUNTERFACTUAL_REPLAY", "LLM_JUDGE", "SPECTRUM"]


class CausalEdge(Struct, kw_only=True):
    project_id: str
    trace_id: str
    source_observation_id: str
    target_observation_id: str
    edge_type: EdgeType
    confidence: float  # 0.0 – 1.0
    method: Method
    evidence: str  # human-readable explanation
    replay_supported: bool  # FRAMEWORK SUPPORTS COUNTERFACTUAL?
    judge_model: str | None = None
    computed_at: datetime
