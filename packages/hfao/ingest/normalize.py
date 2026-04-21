"""OTLP → canonical HFAO Observation normalizer.

SPEC §5. Accepts OpenInference spans, OTel GenAI spans, and plain OTel spans
and returns HFAO ``Observation`` structs with attributes mapped per §5.2
(OTel GenAI) and §5.3 (OpenInference). ``normalize_scores`` extracts any
``gen_ai.evaluation.result`` span events into ``Score`` rows (§5.1).

Priority order when attributes disagree: OpenInference > OTel GenAI > HFAO
default (per §0 reader contract).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, cast

from hfao.schema.events import (
    CostBreakdown,
    Observation,
    ObservationType,
    TokenUsage,
    ToolCall,
)
from hfao.schema.otlp import Span, SpanEvent
from hfao.schema.scores import Score

# ---- OTel GenAI operation.name → HFAO observation type ----

_OTEL_OP_TO_TYPE: dict[str, ObservationType] = {
    "create_agent": "AGENT",
    "invoke_agent": "AGENT",
    "chat": "GENERATION",
    "text_completion": "GENERATION",
    "generate_content": "GENERATION",
    "embeddings": "EMBEDDING",
    "execute_tool": "TOOL",
}

# ---- OpenInference span.kind → HFAO observation type ----

_OI_KIND_TO_TYPE: dict[str, ObservationType] = {
    "LLM": "GENERATION",
    "CHAIN": "SPAN",
    "RETRIEVER": "RETRIEVAL",
    "EMBEDDING": "EMBEDDING",
    "AGENT": "AGENT",
    "TOOL": "TOOL",
    "RERANKER": "RETRIEVAL",
    "EVALUATOR": "EVAL",
    "GUARDRAIL": "GUARDRAIL",
}

# ---- OTel GenAI attribute → model_parameters key ----

_OTEL_MODEL_PARAM_KEYS = {
    "gen_ai.system": "system",
    "gen_ai.request.temperature": "temperature",
    "gen_ai.request.top_p": "top_p",
    "gen_ai.request.top_k": "top_k",
    "gen_ai.request.max_tokens": "max_tokens",
    "gen_ai.request.stop_sequences": "stop_sequences",
    "gen_ai.request.presence_penalty": "presence_penalty",
    "gen_ai.request.frequency_penalty": "frequency_penalty",
}


def normalize(span: Span, *, default_project_id: str = "default") -> list[Observation]:
    """Produce 1+ canonical Observations from a flattened OTLP span.

    - 1:1 in the common case.
    - 1:N reserved for spans carrying multiple events that should split out
      (currently unused; all events stay attached to the parent observation).
    """
    common = _build_common(span, default_project_id=default_project_id)

    if _has_prefix(span.attributes, "openinference."):
        return [_from_openinference(span, common)]
    if _has_prefix(span.attributes, "gen_ai."):
        return [_from_otel_genai(span, common)]
    return [_from_generic(span, common)]


def normalize_scores(span: Span) -> list[Score]:
    """Pull ``gen_ai.evaluation.result`` events off the span into Scores.

    SPEC §5.1: logs with ``gen_ai.evaluation.result`` names are merged into
    the parent span's scores. Span events are treated the same way.
    """
    project_id, trace_id, observation_id = _ids(span, default_project_id="default")
    out: list[Score] = []
    for ev in span.events:
        if ev.name != "gen_ai.evaluation.result":
            continue
        score_name = str(ev.attributes.get("gen_ai.evaluation.name", "evaluation"))
        raw_value = ev.attributes.get("gen_ai.evaluation.score")
        value: float | None
        try:
            value = float(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            value = None
        judge_model = ev.attributes.get("gen_ai.response.model") or ev.attributes.get(
            "gen_ai.request.model"
        )
        explanation = ev.attributes.get("gen_ai.evaluation.explanation")
        out.append(
            Score(
                project_id=project_id,
                trace_id=trace_id,
                observation_id=observation_id,
                name=score_name,
                value=value,
                string_value=None if value is not None else _to_str_or_none(raw_value),
                source="LLM_JUDGE",
                comment=_to_str_or_none(explanation),
                judge_model=_to_str_or_none(judge_model),
                calibration_bias=0.0,
                timestamp=ev.timestamp,
                annotator_id=None,
                eval_run_id=None,
            )
        )
    return out


# ---- helpers ----


def _has_prefix(attrs: dict[str, Any], prefix: str) -> bool:
    return any(k.startswith(prefix) for k in attrs)


def _to_str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v)


def _ids(span: Span, *, default_project_id: str) -> tuple[str, str, str]:
    project_id = (
        span.resource_attributes.get("hfao.project_id")
        or span.attributes.get("hfao.project_id")
        or default_project_id
    )
    trace_id = span.trace_id
    observation_id = span.span_id or str(uuid.uuid4())
    return str(project_id), str(trace_id), str(observation_id)


def _build_common(span: Span, *, default_project_id: str) -> dict[str, Any]:
    project_id, trace_id, observation_id = _ids(span, default_project_id=default_project_id)
    env = (
        span.resource_attributes.get("deployment.environment")
        or span.attributes.get("deployment.environment")
        or span.resource_attributes.get("hfao.environment")
        or span.attributes.get("hfao.environment")
        or "production"
    )
    release = (
        span.resource_attributes.get("service.version")
        or span.attributes.get("service.version")
        or span.resource_attributes.get("hfao.release")
        or span.attributes.get("hfao.release")
    )
    start = span.start_time
    end = span.end_time
    duration = int((end - start).total_seconds() * 1000) if end is not None else None
    status: str = span.status
    status_message = span.status_message
    session_id = _resolve_session_id(span.attributes, span.resource_attributes)
    user_id = (
        span.attributes.get("user.id")
        or span.attributes.get("enduser.id")
        or span.resource_attributes.get("user.id")
    )
    tags_raw: Any = (
        span.attributes.get("tag.tags") or span.attributes.get("hfao.tags") or []
    )
    if isinstance(tags_raw, str):
        tags = [tags_raw]
    else:
        tags = [str(t) for t in cast("list[Any]", tags_raw)]
    metadata = _extract_metadata(span.attributes)
    return {
        "project_id": project_id,
        "trace_id": trace_id,
        "observation_id": observation_id,
        "parent_observation_id": span.parent_span_id,
        "session_id": _to_str_or_none(session_id),
        "user_id": _to_str_or_none(user_id),
        "environment": str(env),
        "release": _to_str_or_none(release),
        "start_time": start,
        "end_time": end,
        "duration_ms": duration,
        "status": status if status in ("ok", "error", "unset") else "unset",
        "status_message": status_message,
        "tags": tags,
        "metadata": metadata,
    }


def _resolve_session_id(
    attrs: dict[str, Any], resource_attrs: dict[str, Any]
) -> str | None:
    # §5.5: A2A contextId maps to session_id; also OpenInference session.id
    # and OTel gen_ai.conversation.id.
    for key in (
        "session.id",
        "gen_ai.conversation.id",
        "a2a.context_id",
        "hfao.session_id",
    ):
        v = attrs.get(key) or resource_attrs.get(key)
        if v:
            return str(v)
    return None


def _extract_metadata(attrs: dict[str, Any]) -> dict[str, str]:
    """Collect OpenInference ``metadata`` and a few explicit A2A keys."""
    md: dict[str, str] = {}
    # OpenInference `metadata` attribute can be JSON string or dict.
    raw = attrs.get("metadata")
    if isinstance(raw, str):
        try:
            parsed: Any = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in cast("dict[Any, Any]", parsed).items():
                    md[str(k)] = _to_str_or_none(v) or ""
        except json.JSONDecodeError:
            md["metadata"] = raw
    elif isinstance(raw, dict):
        for k, v in cast("dict[Any, Any]", raw).items():
            md[str(k)] = _to_str_or_none(v) or ""
    for k in ("a2a.task_id", "a2a.agent_card_url"):
        if k in attrs:
            md[k] = _to_str_or_none(attrs[k]) or ""
    return md


def _empty_usage() -> TokenUsage:
    return TokenUsage()


def _empty_cost() -> CostBreakdown:
    return CostBreakdown()


# ---- OTel GenAI mapping (§5.2) ----


def _from_otel_genai(span: Span, common: dict[str, Any]) -> Observation:
    attrs = span.attributes
    op_name = _to_str_or_none(attrs.get("gen_ai.operation.name")) or span.name
    obs_type: ObservationType = _OTEL_OP_TO_TYPE.get(op_name, "SPAN")
    model = _to_str_or_none(
        attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
    )

    model_params: dict[str, str] = {}
    for otel_key, canonical_key in _OTEL_MODEL_PARAM_KEYS.items():
        if otel_key in attrs:
            model_params[canonical_key] = _to_str_or_none(attrs[otel_key]) or ""

    usage = TokenUsage(
        prompt_tokens=int(attrs.get("gen_ai.usage.input_tokens") or 0),
        completion_tokens=int(attrs.get("gen_ai.usage.output_tokens") or 0),
        cache_read_tokens=int(
            attrs.get("gen_ai.usage.cache_read.input_tokens") or 0
        ),
        cache_creation_tokens=int(
            attrs.get("gen_ai.usage.cache_creation.input_tokens") or 0
        ),
        total_tokens=int(attrs.get("gen_ai.usage.total_tokens") or 0),
    )
    if usage.total_tokens == 0 and (usage.prompt_tokens or usage.completion_tokens):
        usage = TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            total_tokens=usage.prompt_tokens + usage.completion_tokens,
        )

    input_json = _to_str_or_none(attrs.get("gen_ai.input.messages"))
    output_json = _to_str_or_none(attrs.get("gen_ai.output.messages"))

    tool_calls = _collect_otel_tool_calls(attrs)
    tool_call_names = [tc.name for tc in tool_calls]

    agent_id = _to_str_or_none(attrs.get("gen_ai.agent.id"))
    agent_role = _to_str_or_none(attrs.get("gen_ai.agent.name"))

    return Observation(
        name=op_name,
        type=obs_type,
        input=input_json,
        output=output_json,
        model=model,
        model_parameters=model_params,
        usage=usage,
        cost=_empty_cost(),
        tool_calls=tool_calls,
        tool_call_names=tool_call_names,
        agent_id=agent_id,
        agent_role=agent_role,
        handoff_target_agent_id=_to_str_or_none(
            attrs.get("gen_ai.handoff.target_agent.id")
        ),
        event_version=1,
        ingested_at=datetime.now(timezone.utc),
        **common,
    )


def _collect_otel_tool_calls(attrs: dict[str, Any]) -> list[ToolCall]:
    if "gen_ai.tool.name" in attrs:
        return [
            ToolCall(
                id=str(attrs.get("gen_ai.tool.call.id") or ""),
                name=str(attrs["gen_ai.tool.name"]),
                arguments=_to_str_or_none(attrs.get("gen_ai.tool.call.arguments"))
                or "",
                result=_to_str_or_none(attrs.get("gen_ai.tool.call.result")),
                error=_to_str_or_none(attrs.get("gen_ai.tool.call.error")),
            )
        ]
    return []


# ---- OpenInference mapping (§5.3) ----


def _from_openinference(span: Span, common: dict[str, Any]) -> Observation:
    attrs = span.attributes
    kind_raw = _to_str_or_none(attrs.get("openinference.span.kind")) or ""
    obs_type: ObservationType = _OI_KIND_TO_TYPE.get(kind_raw.upper(), "SPAN")

    model = _to_str_or_none(attrs.get("llm.model_name"))
    model_params: dict[str, str] = {}
    invocation = attrs.get("llm.invocation_parameters")
    if isinstance(invocation, str):
        try:
            parsed_inv: Any = json.loads(invocation)
            if isinstance(parsed_inv, dict):
                model_params = {
                    str(k): _to_str_or_none(v) or ""
                    for k, v in cast("dict[Any, Any]", parsed_inv).items()
                }
        except json.JSONDecodeError:
            model_params = {"raw": invocation}
    elif isinstance(invocation, dict):
        model_params = {
            str(k): _to_str_or_none(v) or ""
            for k, v in cast("dict[Any, Any]", invocation).items()
        }

    usage = TokenUsage(
        prompt_tokens=int(attrs.get("llm.token_count.prompt") or 0),
        completion_tokens=int(attrs.get("llm.token_count.completion") or 0),
        cache_read_tokens=int(attrs.get("llm.token_count.prompt_cache.read") or 0),
        cache_creation_tokens=int(
            attrs.get("llm.token_count.prompt_cache.write") or 0
        ),
        total_tokens=int(attrs.get("llm.token_count.total") or 0),
    )
    if usage.total_tokens == 0 and (usage.prompt_tokens or usage.completion_tokens):
        usage = TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            total_tokens=usage.prompt_tokens + usage.completion_tokens,
        )

    input_val = _to_str_or_none(attrs.get("input.value"))
    output_val = _to_str_or_none(attrs.get("output.value"))
    if input_val is None:
        input_val = _reconstruct_oi_messages(attrs, prefix="llm.input_messages")
    if output_val is None:
        output_val = _reconstruct_oi_messages(attrs, prefix="llm.output_messages")

    tool_calls = _collect_openinference_tool_calls(attrs)
    tool_call_names = [tc.name for tc in tool_calls]

    agent_id = _to_str_or_none(attrs.get("agent.id") or attrs.get("gen_ai.agent.id"))
    agent_role = _to_str_or_none(
        attrs.get("agent.name") or attrs.get("gen_ai.agent.name")
    )

    return Observation(
        name=span.name,
        type=obs_type,
        input=input_val,
        output=output_val,
        model=model,
        model_parameters=model_params,
        usage=usage,
        cost=_empty_cost(),
        tool_calls=tool_calls,
        tool_call_names=tool_call_names,
        agent_id=agent_id,
        agent_role=agent_role,
        handoff_target_agent_id=_to_str_or_none(attrs.get("openinference.handoff.target")),
        event_version=1,
        ingested_at=datetime.now(timezone.utc),
        **common,
    )


def _reconstruct_oi_messages(attrs: dict[str, Any], *, prefix: str) -> str | None:
    messages: list[dict[str, Any]] = []
    # Collect indexed message attrs like llm.input_messages.0.message.role
    indices: set[int] = set()
    for k in attrs:
        if k.startswith(prefix + "."):
            rest = k[len(prefix) + 1 :]
            idx_str, _, _ = rest.partition(".")
            if idx_str.isdigit():
                indices.add(int(idx_str))
    if not indices:
        return None
    for i in sorted(indices):
        msg_prefix = f"{prefix}.{i}.message"
        role = attrs.get(f"{msg_prefix}.role")
        content = attrs.get(f"{msg_prefix}.content")
        messages.append(
            {"role": _to_str_or_none(role) or "", "content": _to_str_or_none(content) or ""}
        )
    return json.dumps(messages) if messages else None


def _collect_openinference_tool_calls(attrs: dict[str, Any]) -> list[ToolCall]:
    out: list[ToolCall] = []
    if "tool.name" in attrs:
        out.append(
            ToolCall(
                id=str(attrs.get("tool.call.id") or ""),
                name=str(attrs["tool.name"]),
                arguments=_to_str_or_none(attrs.get("tool.parameters")) or "",
                result=_to_str_or_none(attrs.get("tool.result")),
                error=_to_str_or_none(attrs.get("tool.error")),
            )
        )
    # Also collect indexed llm.output_messages.*.tool_calls.*
    indices: set[tuple[int, int]] = set()
    for k in attrs:
        if k.startswith("llm.output_messages."):
            parts = k.split(".")
            if len(parts) >= 6 and parts[3] == "message" and parts[4] == "tool_calls":
                msg_i = int(parts[2]) if parts[2].isdigit() else -1
                tc_i = int(parts[5]) if parts[5].isdigit() else -1
                if msg_i >= 0 and tc_i >= 0:
                    indices.add((msg_i, tc_i))
    for msg_i, tc_i in sorted(indices):
        p = f"llm.output_messages.{msg_i}.message.tool_calls.{tc_i}.tool_call"
        name = attrs.get(f"{p}.function.name")
        if not name:
            continue
        out.append(
            ToolCall(
                id=str(attrs.get(f"{p}.id") or ""),
                name=str(name),
                arguments=_to_str_or_none(attrs.get(f"{p}.function.arguments")) or "",
            )
        )
    return out


# ---- Generic / unknown span (§5.7) ----


def _from_generic(span: Span, common: dict[str, Any]) -> Observation:
    return Observation(
        name=span.name,
        type="SPAN",
        usage=_empty_usage(),
        cost=_empty_cost(),
        event_version=1,
        ingested_at=datetime.now(timezone.utc),
        **common,
    )


__all__ = ["normalize", "normalize_scores", "Span", "SpanEvent"]
