# Hugging Face smolagents quickstart

smolagents' distinguishing feature is `CodeAgent`: the agent generates Python
code as an action and executes it in a sandboxed interpreter. HFAO's
`transformers_agents_extra` wraps those code executions in
`openinference.span.kind=TOOL` spans with the code payload as `input.value`,
and marks smolagents traces `replay_supported=False` per §12.2.

For all other smolagents span types (agent / step / LLM / tool),
`openinference-instrumentation-smolagents` does the mapping.

## Install

> **`hfao` is not on PyPI yet.** Install HFAO from source:
> `git clone https://github.com/OtotaO/HFAO.git && cd HFAO && uv sync`,
> then install this quickstart's other packages, which are on PyPI today:
> `pip install smolagents openinference-instrumentation-smolagents`.
> The single command below works once the HFAO publish lands.

```bash
pip install hfao smolagents openinference-instrumentation-smolagents
```

## Minimal app

```python
import hfao
from smolagents import CodeAgent, HfApiModel
from hfao.instrumentations import transformers_agents_extra

hfao.init(project="smol-demo")

agent = CodeAgent(model=HfApiModel(), tools=[])

with hfao.session(user_id="alice"):
    result = agent.run("What is the capital of France?")
    print(result)
```

## Wrapping a hand-rolled code action

If you drive the Python interpreter yourself (e.g. inside a custom
`CodeAgent.step()`), wrap the execution to emit a canonical `TOOL`
observation:

```python
code = "capitals['France']"
with transformers_agents_extra.using_code_action(code, agent_name="demo") as span:
    result = python_interpreter(code)
    span.set_attribute("output.value", str(result))
```

## What lands in the observatory

- One `AGENT` observation for the top-level `agent.run(...)` call.
- One `CHAIN` observation per reasoning step.
- One `TOOL` observation per code action with `tool.name="python_interpreter"`,
  `input.value = <code>`, and `hfao.replay_supported = False`.
- Token usage + cost are populated from the HF Inference Provider response
  when you use `HfApiModel`.

## Replay status

smolagents does not expose a resume-from-checkpoint API in v1. Cockpit's
replay button is disabled for these traces — expected and spec-compliant
(§12.2: *replay unsupported*). Re-run the full prompt instead.
