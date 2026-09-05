# OpenAI Agents SDK quickstart

The OpenAI Agents SDK ships its own in-process trace tree (HandoffSpan,
GuardrailSpan, MCPListToolsSpan, FunctionSpan, GenerationSpan, …). HFAO's
`HFAOTracingProcessor` consumes those via `agents.set_trace_processors(...)`
and re-emits them as OTel spans with OpenInference attributes so the canonical
observation mapping kicks in.

## Install

> **`hfao` is not on PyPI yet.** Install HFAO from source:
> `git clone https://github.com/OtotaO/HFAO.git && cd HFAO && uv sync`,
> then install this quickstart's other packages, which are on PyPI today:
> `pip install openai-agents openinference-instrumentation-openai-agents`.
> The single command below works once the HFAO publish lands.

```bash
pip install hfao openai-agents openinference-instrumentation-openai-agents
```

## Minimal app

```python
import hfao
from agents import Agent, Runner, set_trace_processors
from hfao.instrumentations import openai_agents_extra

hfao.init(project="my-agent")
set_trace_processors([openai_agents_extra.HFAOTracingProcessor()])

triage = Agent(
    name="Triage",
    instructions="Route the user to billing or support.",
    handoffs=["billing", "support"],
)

with hfao.session(user_id="alice"):
    result = Runner.run_sync(triage, "I was charged twice for my subscription.")
    print(result.final_output)
```

Or rely on the one-liner installer:

```python
openai_agents_extra.install()  # no-op if `agents` is not importable
```

## What lands in the observatory

- `HandoffSpan` → canonical `HANDOFF` observation with
  `handoff_target_agent_id` populated.
- `GuardrailSpan` → `GUARDRAIL` observation with `guardrail.name` +
  `guardrail.triggered`.
- `MCPListToolsSpan` → `TOOL` observation with `tool.name="mcp.list_tools"`
  and `mcp.tools.count`.
- `FunctionSpan` / `GenerationSpan` → canonical `TOOL` / `GENERATION` per §5.3.

All spans inside a `hfao.session(...)` block carry `session_id`; handoffs
preserve the session across agent transitions automatically.
