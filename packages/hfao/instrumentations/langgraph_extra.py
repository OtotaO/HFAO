"""LangGraph → HFAO session mapping extra.

SPEC §12.2. LangGraph emits spans via LangChain callbacks; the
``openinference-instrumentation-langchain`` package handles the
attribute mapping. What it does *not* do is lift LangGraph's
per-invocation ``thread_id`` — the checkpoint identifier passed via
``RunnableConfig.configurable.thread_id`` — onto the canonical HFAO
``session_id``. Without that lift, every LangGraph thread looks like a
separate session in the observatory.

This module supplies two surfaces to close the gap:

- :func:`using_thread` — a context manager that attaches a LangGraph
  thread id to OTel baggage. Pair it with :func:`hfao.session` (or use
  standalone) before calling into a LangGraph graph:

  .. code-block:: python

      from hfao.instrumentations import langgraph_extra

      with langgraph_extra.using_thread("thread-42"):
          result = graph.invoke(state, config={"configurable": {"thread_id": "thread-42"}})

- :func:`install` — registers :class:`LangGraphThreadSpanProcessor` on
  the active :class:`TracerProvider`. The processor reads the baggage
  key on every new span and stamps ``session.id`` /
  ``gen_ai.conversation.id`` so the §5.2/§5.3 normalizer resolves
  ``session_id`` for every observation under the thread.

Safe when ``langgraph`` / ``langchain`` are not importable: both
surfaces are pure OTel and do not touch LangGraph internals.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import baggage, trace
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace import Span as SDKSpan

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider


_BAGGAGE_THREAD_ID = "hfao.langgraph.thread_id"
_SESSION_ID_ATTR = "session.id"
_CONVERSATION_ID_ATTR = "gen_ai.conversation.id"


class LangGraphThreadSpanProcessor(SpanProcessor):
    """Mirror LangGraph ``thread_id`` baggage → HFAO session attributes."""

    def on_start(self, span: SDKSpan, parent_context: Context | None = None) -> None:
        raw = baggage.get_baggage(_BAGGAGE_THREAD_ID, parent_context)
        if raw is None:
            return
        thread_id = str(raw)
        existing = span.attributes or {}
        if _SESSION_ID_ATTR not in existing:
            span.set_attribute(_SESSION_ID_ATTR, thread_id)
        if _CONVERSATION_ID_ATTR not in existing:
            span.set_attribute(_CONVERSATION_ID_ATTR, thread_id)

    def on_end(self, span: ReadableSpan) -> None:
        return

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


def install(tracer_provider: TracerProvider | None = None) -> LangGraphThreadSpanProcessor:
    """Register :class:`LangGraphThreadSpanProcessor` on the provider.

    Passing ``None`` uses the globally-installed provider from
    :func:`hfao.init`. Returns the processor instance so callers can
    shut it down in teardown.
    """
    proc = LangGraphThreadSpanProcessor()
    provider = tracer_provider if tracer_provider is not None else trace.get_tracer_provider()
    add = getattr(provider, "add_span_processor", None)
    if add is None:
        raise RuntimeError(
            "langgraph_extra.install() requires an SDK TracerProvider; "
            "did you call hfao.init() before this?"
        )
    add(proc)
    return proc


@contextmanager
def using_thread(thread_id: str) -> Iterator[None]:
    """Attach ``thread_id`` to the active OTel context via baggage.

    Inside the ``with`` block every span opened — including those
    produced by the OpenInference LangChain instrumentor — gets
    ``session.id`` / ``gen_ai.conversation.id`` set to ``thread_id`` by
    :class:`LangGraphThreadSpanProcessor`.
    """
    ctx = baggage.set_baggage(_BAGGAGE_THREAD_ID, thread_id)
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


__all__ = ["LangGraphThreadSpanProcessor", "install", "using_thread"]
