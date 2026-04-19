from __future__ import annotations

from datetime import datetime
from typing import Literal

from msgspec import Struct, field

ObservationType = Literal[
    "AGENT",
    "GENERATION",
    "TOOL",
    "RETRIEVAL",
    "EMBEDDING",
    "EVAL",
    "GUARDRAIL",
    "HANDOFF",
    "SPAN",
    "EVENT",
]

Status = Literal["ok", "error", "unset"]

Level = Literal["DEFAULT", "DEBUG", "WARNING", "ERROR"]


class TokenUsage(Struct, frozen=True, kw_only=True):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0


class CostBreakdown(Struct, frozen=True, kw_only=True):
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    total_cost_usd: float = 0.0


class ToolCall(Struct, frozen=True, kw_only=True):
    id: str
    name: str
    arguments: str  # JSON string; keep as string at storage layer
    result: str | None = None
    error: str | None = None


class Observation(Struct, kw_only=True):
    # Identity
    project_id: str
    trace_id: str
    observation_id: str
    parent_observation_id: str | None = None

    # Routing / context
    session_id: str | None = None  # OpenInference session.id / OTel gen_ai.conversation.id
    user_id: str | None = None
    environment: str = "production"
    release: str | None = None

    # What
    name: str
    type: ObservationType
    level: Level = "DEFAULT"

    # When
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: int | None = None

    # Status
    status: Status = "unset"
    status_message: str | None = None

    # Payload (large bodies offloaded; see §6)
    input: str | None = None  # JSON string OR pointer ref
    output: str | None = None  # JSON string OR pointer ref
    input_ref: str | None = None  # s3://... if offloaded
    output_ref: str | None = None

    # Generation-specific
    model: str | None = None
    model_parameters: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost: CostBreakdown = field(default_factory=CostBreakdown)

    # Tool-specific
    tool_definitions: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    tool_calls: list[ToolCall] = field(default_factory=lambda: cast(list[ToolCall], []))
    tool_call_names: list[str] = field(default_factory=lambda: cast(list[str], []))

    # Agent-specific
    agent_id: str | None = None
    agent_role: str | None = None
    handoff_target_agent_id: str | None = None

    # Prompt linkage
    prompt_name: str | None = None
    prompt_version: int | None = None
    prompt_label: str | None = None

    # Free-form
    metadata: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    tags: list[str] = field(default_factory=lambda: cast(list[str], []))

    # Bookkeeping
    event_version: int = 1
    ingested_at: datetime


from typing import cast
