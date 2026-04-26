"""HF Transformers Agents / smolagents → HFAO extras.

SPEC §12.2. The ``openinference-instrumentation-smolagents`` package
covers agent/step/LLM/tool spans; smolagents' distinguishing feature is
the ``CodeAgent`` that executes generated Python in its own interpreter.
This extra:

- :func:`using_code_action` — a context manager that wraps a code
  execution in an OTel span tagged with
  ``openinference.span.kind=TOOL`` + ``tool.name=python_interpreter``
  and an ``input.value`` carrying the code string. The normalizer
  (§5.3) turns the span into a canonical ``TOOL`` observation with
  ``tool_call_names=["python_interpreter"]``.
- :func:`mark_replay_unsupported` — sets ``hfao.replay_supported=False``
  on the current span. smolagents replay is not wired up in v1
  (§12.2 note); the attribute preserves that signal on the wire so
  Cockpit's replay button grays out correctly.

Both helpers are importable even when ``smolagents`` is not installed;
neither touches smolagents internals.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.trace import Span

_TRACER_NAME = "hfao.instrumentations.transformers_agents"


@contextmanager
def using_code_action(
    code: str,
    *,
    agent_name: str | None = None,
    language: str = "python",
) -> Iterator[Span]:
    """Open an observation span for a single CodeAgent code execution.

    .. code-block:: python

        from hfao.instrumentations import transformers_agents_extra

        with transformers_agents_extra.using_code_action(code) as span:
            result = python_interpreter(code)
            span.set_attribute("output.value", str(result))

    Exceptions raised inside the block mark the span as ``ERROR`` and
    record the exception before re-raising.
    """
    tracer = trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(
        name="smolagents.code_action",
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": "python_interpreter",
            "tool.parameters": code,
            "input.value": code,
            "input.mime_type": f"text/x-{language}",
            "hfao.replay_supported": False,
            "hfao.agent.name": agent_name or "",
        },
    ) as span:
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def mark_replay_unsupported() -> None:
    """Tag the current span with ``hfao.replay_supported=False`` (§12.2 note)."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("hfao.replay_supported", False)


__all__ = ["using_code_action", "mark_replay_unsupported"]
