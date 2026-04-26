"""HFAO Cockpit — trace-as-chat component.

SPEC §10.3. Maps a list of canonical :class:`Observation` rows to the
``gr.Chatbot`` message tuples Gradio 6 expects, with HFAO-specific
metadata folded into accordion-friendly markdown so the user can
inspect tool calls, agent transitions, and CoT inline.

Shape returned:

    [(user_text_or_None, assistant_text_or_None), ...]

The mapping deliberately does not require Gradio at import time so
acceptance tests can call :func:`build_messages` directly with a list
of dicts.
"""

from __future__ import annotations

import json
from typing import Any

# Mapping rules per SPEC §10.3:
#   TOOL  → "🔧 tool_name(args)" assistant turn + accordion result
#   AGENT → "◆ agent_name" assistant turn (badge)
#   HANDOFF → "→ target_agent_id" assistant turn (arrow)
#   GENERATION → assistant turn carrying the model output
#   GUARDRAIL → "⛨ guardrail_name (triggered=…)" assistant turn
#   RETRIEVAL / EMBEDDING / EVAL / SPAN → folded into a small accordion
ChatTurn = tuple[str | None, str | None]


def build_messages(observations: list[dict[str, Any]]) -> list[ChatTurn]:
    """Build the ``gr.Chatbot`` message list from observation rows.

    Sorted by start_time. ``input`` strings on root-level GENERATION /
    AGENT / TOOL observations become user turns when they read like a
    user message (string or {"role":"user",...} JSON); everything else
    is assistant content.
    """
    if not observations:
        return [(None, "_(no observations in this trace)_")]

    sorted_obs = sorted(observations, key=lambda o: str(o.get("start_time", "")))
    turns: list[ChatTurn] = []
    for obs in sorted_obs:
        user, assistant = _render_observation(obs)
        if user is None and assistant is None:
            continue
        turns.append((user, assistant))
    return turns or [(None, "_(no displayable observations)_")]


def _render_observation(obs: dict[str, Any]) -> ChatTurn:
    obs_type = str(obs.get("type", "SPAN"))
    name = str(obs.get("name", ""))
    if obs_type == "GENERATION":
        return (_extract_user_text(obs), _generation_markdown(obs))
    if obs_type == "TOOL":
        return (None, _tool_markdown(obs))
    if obs_type == "AGENT":
        return (None, f"◆ **agent**: `{name}`")
    if obs_type == "HANDOFF":
        target = obs.get("handoff_target_agent_id") or "?"
        return (None, f"→ **handoff** → `{target}`")
    if obs_type == "GUARDRAIL":
        return (None, _guardrail_markdown(obs, name))
    if obs_type in ("RETRIEVAL", "EMBEDDING", "EVAL"):
        return (None, _details_accordion(obs_type.lower(), name, _payload(obs)))
    return (None, None)


def _extract_user_text(obs: dict[str, Any]) -> str | None:
    raw = obs.get("input")
    if not raw:
        return None
    return _maybe_extract_user_role(raw)


def _maybe_extract_user_role(raw: object) -> str | None:
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        return _maybe_extract_user_role(decoded)
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content")
                if isinstance(content, str):
                    return content
        return None
    if isinstance(raw, dict):
        if raw.get("role") == "user":
            content = raw.get("content")
            if isinstance(content, str):
                return content
        return None
    return None


def _generation_markdown(obs: dict[str, Any]) -> str:
    model = obs.get("model") or "?"
    out = obs.get("output") or "_(no output)_"
    usage = obs.get("usage") or {}
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    cost = (obs.get("cost") or {}) if isinstance(obs.get("cost"), dict) else {}
    cost_total = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    badge = f"`{model}`"
    if total_tokens:
        badge += f" · {int(total_tokens)} tok"
    if cost_total:
        badge += f" · ${float(cost_total):.4f}"
    body = out if isinstance(out, str) else json.dumps(out)
    return f"✨ {badge}\n\n{body}"


def _tool_markdown(obs: dict[str, Any]) -> str:
    tool_calls = obs.get("tool_calls") or []
    if isinstance(tool_calls, list) and tool_calls:
        parts: list[str] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            args = call.get("arguments")
            args_pretty = _pretty(args) if isinstance(args, str) else json.dumps(args, indent=2)
            result = call.get("result")
            error = call.get("error")
            parts.append(
                f"<details><summary>🔧 <code>{_safe(call.get('name'))}</code> "
                f"<small>id={_safe(call.get('id'))}</small></summary>\n\n"
                f"**args**\n```json\n{args_pretty}\n```\n"
                + (f"**result**\n```\n{_safe(result)}\n```\n" if result else "")
                + (f"**error**\n```\n{_safe(error)}\n```\n" if error else "")
                + "</details>"
            )
        return "\n\n".join(parts)
    name = obs.get("name") or "tool"
    return f"🔧 <code>{_safe(name)}</code>"


def _guardrail_markdown(obs: dict[str, Any], name: str) -> str:
    metadata = obs.get("metadata")
    triggered = (
        metadata.get("guardrail.triggered") if isinstance(metadata, dict) else None
    )
    suffix = "" if triggered is None else f" (triggered={triggered})"
    return f"⛨ **guardrail**: `{name}`{suffix}"


def _details_accordion(kind: str, name: str, payload: str) -> str:
    return (
        f"<details><summary><strong>{kind}</strong>: {_safe(name)}</summary>\n\n"
        f"```json\n{payload}\n```\n</details>"
    )


def _payload(obs: dict[str, Any]) -> str:
    raw = obs.get("output") or obs.get("input") or "{}"
    return _pretty(raw if isinstance(raw, str) else json.dumps(raw))


def _pretty(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return raw


def _safe(value: Any) -> str:  # noqa: ANN401 — payload boundary
    return "" if value is None else str(value)


__all__ = ["build_messages", "ChatTurn"]
