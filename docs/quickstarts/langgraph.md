# LangGraph quickstart

LangGraph emits spans via LangChain callbacks. HFAO auto-picks them up through
`openinference-instrumentation-langchain` and maps each run's
`config.configurable.thread_id` onto the canonical `session_id` via
`hfao.instrumentations.langgraph_extra`.

## Install

```bash
pip install hfao langgraph openinference-instrumentation-langchain
```

## Minimal app

```python
import hfao
from hfao.instrumentations import langgraph_extra
from langgraph.graph import StateGraph, END

hfao.init(project="my-langgraph-app")
langgraph_extra.install()

def respond(state: dict) -> dict:
    return {"answer": f"Echo: {state['question']}"}

graph = StateGraph(dict)
graph.add_node("respond", respond)
graph.set_entry_point("respond")
graph.add_edge("respond", END)
compiled = graph.compile()

thread_id = "thread-42"
with langgraph_extra.using_thread(thread_id):
    result = compiled.invoke(
        {"question": "capital of France?"},
        config={"configurable": {"thread_id": thread_id}},
    )
print(result)
```

## What lands in the observatory

- One canonical `AGENT` observation for the compiled graph.
- One `GENERATION` observation per LLM node (when the node calls an LLM).
- Every observation carries `session_id = "thread-42"` — the same thread id you
  pass to LangGraph's checkpointer — so a thread's full history stays grouped
  under a single session in Cockpit.

## Optional: hfao.session() wrapping

If your service already opens an HFAO session per user turn, nest LangGraph
inside it. The innermost session id wins; LangGraph's thread id takes
precedence only when no HFAO session is active.

```python
with hfao.session(session_id=thread_id, user_id=current_user):
    compiled.invoke(state, config={"configurable": {"thread_id": thread_id}})
```
