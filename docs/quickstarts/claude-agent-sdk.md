# Claude Agent SDK quickstart

The Claude Agent SDK exposes hooks (`PreToolUse`, `PostToolUse`, `SessionStart`,
`UserPromptSubmit`, `Stop`, `Notification`, …). HFAO's
`claude_agent_extra.build_hooks()` returns a ready-made hook bundle that stamps
canonical tool-call attributes onto the active span. Pair it with
`openinference-instrumentation-claude-agent-sdk` for the base LLM + tool span
mapping.

Replay is supported: Cockpit's replay button re-runs a trace via
`claude_agent_sdk.query(resume_from=trace_id, ...)`.

## Install

```bash
pip install hfao claude-agent-sdk openinference-instrumentation-claude-agent-sdk
```

## Minimal app

```python
import hfao
from claude_agent_sdk import query, ClaudeAgentOptions
from hfao.instrumentations import claude_agent_extra

hfao.init(project="my-agent")

opts = ClaudeAgentOptions(
    system_prompt="You are a helpful assistant.",
    hooks=claude_agent_extra.build_hooks(),
)

with hfao.session(user_id="alice"):
    async for message in query(prompt="Summarise the latest PR.", options=opts):
        print(message)
```

## Merging with your own hooks

HFAO's hooks run first on each event; your callbacks run after, so they can
read whatever HFAO set without being clobbered:

```python
from claude_agent_sdk import HookMatcher

my_hooks = {"PreToolUse": [HookMatcher(hooks=[my_audit_callback])]}
opts = ClaudeAgentOptions(hooks=claude_agent_extra.build_hooks(extra_hooks=my_hooks))
```

## What lands in the observatory

- `PreToolUse` → `claude_agent.pre_tool_use` span event with `tool.name`,
  `tool.call_id`, `tool.arguments` (JSON).
- `PostToolUse` → `claude_agent.post_tool_use` event with `tool.result`.
- `SessionStart` → `claude_agent.session.source` attribute on the root span.
- Each `Message` turn → a canonical `GENERATION` observation via the
  OpenInference instrumentor, with token usage + cost from the API response.
- Every observation carries `session_id` when wrapped in `hfao.session(...)`.
