"""Claude Agent SDK → HFAO hooks extra.

SPEC §12.2. The Claude Agent SDK exposes a hook system
(``PreToolUse``, ``PostToolUse``, ``UserPromptSubmit``, ``Stop``,
``Notification``, ``SessionStart``, ``SessionEnd``, …). This module
supplies a ready-made hook bundle that stamps HFAO-canonical attributes
onto the currently-active OTel span whenever Claude Agent SDK invokes
a tool, so the ``openinference-instrumentation-claude-agent-sdk`` traces
line up with the §4.1 ``tool_call_names`` / ``tool_calls`` fields.

Usage (per §12.2):

    import hfao
    from claude_agent_sdk import ClaudeAgentOptions
    from hfao.instrumentations import claude_agent_extra

    hfao.init(project="my-agent")
    opts = ClaudeAgentOptions(hooks=claude_agent_extra.build_hooks())

Merging with user-owned hooks::

    user_hooks = {"PreToolUse": [HookMatcher(hooks=[my_cb])]}
    opts = ClaudeAgentOptions(
        hooks=claude_agent_extra.build_hooks(extra_hooks=user_hooks),
    )

``resume_from=True`` in :func:`hfao.ingest.normalize` already marks
Claude Agent traces as ``replay_supported`` (§12.2 note). This module
only adds per-tool-call attributes; nothing here affects replay.
"""

from __future__ import annotations

import json
from typing import Any

from opentelemetry import trace


async def _hfao_pre_tool_use(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,  # noqa: ARG001, ANN401 — SDK callback
) -> dict[str, Any]:
    span = trace.get_current_span()
    if span.is_recording():
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input")
        span.add_event(
            name="claude_agent.pre_tool_use",
            attributes={
                "tool.name": tool_name,
                "tool.call_id": tool_use_id or "",
                "tool.arguments": _serialize(tool_input),
            },
        )
    return {}


async def _hfao_post_tool_use(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,  # noqa: ARG001, ANN401 — SDK callback
) -> dict[str, Any]:
    span = trace.get_current_span()
    if span.is_recording():
        tool_name = str(input_data.get("tool_name", ""))
        tool_response = input_data.get("tool_response")
        span.add_event(
            name="claude_agent.post_tool_use",
            attributes={
                "tool.name": tool_name,
                "tool.call_id": tool_use_id or "",
                "tool.result": _serialize(tool_response),
            },
        )
    return {}


async def _hfao_session_start(
    input_data: dict[str, Any],
    tool_use_id: str | None,  # noqa: ARG001
    context: Any,  # noqa: ARG001, ANN401 — SDK callback
) -> dict[str, Any]:
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("claude_agent.session.source", str(input_data.get("source", "")))
    return {}


_HOOK_TABLE: dict[str, Any] = {
    "PreToolUse": _hfao_pre_tool_use,
    "PostToolUse": _hfao_post_tool_use,
    "SessionStart": _hfao_session_start,
}


def build_hooks(
    extra_hooks: dict[str, list[Any]] | None = None,
) -> dict[str, list[Any]]:
    """Build a hooks dict for :class:`ClaudeAgentOptions`.

    Returns a mapping of hook-event name → list of ``HookMatcher``
    instances. When ``claude_agent_sdk`` is not importable, falls back
    to raw callables (the SDK accepts both forms). ``extra_hooks`` is
    merged alongside HFAO's entries; user callbacks run after HFAO's,
    so HFAO's attributes are never clobbered by third-party hooks.
    """
    matcher_cls: Any = _import_hook_matcher()
    out: dict[str, list[Any]] = {}
    for event, callback in _HOOK_TABLE.items():
        matcher: Any = matcher_cls(hooks=[callback]) if matcher_cls is not None else callback
        out[event] = [matcher]

    if extra_hooks:
        for event, matchers in extra_hooks.items():
            out.setdefault(event, []).extend(matchers)
    return out


def _import_hook_matcher() -> Any:  # noqa: ANN401 — SDK boundary
    # claude_agent_sdk ships its own stubs; pyright can't see them when
    # the package isn't installed. Silence the missing-import and let
    # the callable return Any so build_hooks() stays strict-clean.
    try:
        from claude_agent_sdk import HookMatcher  # type: ignore[import-not-found, import-untyped]  # noqa: I001
    except ImportError:
        return None
    return HookMatcher  # type: ignore[no-any-return]


def _serialize(value: Any) -> str:  # noqa: ANN401 — SDK payload boundary
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


__all__ = ["build_hooks"]
