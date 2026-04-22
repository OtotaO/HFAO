"""HFAO SDK user-facing context helpers.

SPEC §12.1. ``HFAOContext`` is the object returned by :func:`hfao.init` and
also exposes the module-level helpers ``hfao.session`` and ``hfao.prompt``.

Session identity propagation works in two layers:

1. The :func:`session` context manager opens a parent span and sets
   OpenTelemetry *baggage* (``session.id`` / ``user.id`` /
   ``gen_ai.conversation.id``) on the active context.
2. :class:`HFAOBaggageSpanProcessor`, registered by :func:`hfao.init`,
   reads those baggage keys at span-start time and stamps them as
   attributes on every descendant span. This is what makes
   ``with hfao.session(user_id="alice"): agent.run(...)`` tag every
   LLM / tool / chain span produced by auto-instrumented frameworks,
   not just the ``hfao.session`` root span.

The normalizer in ``hfao.ingest.normalize`` then lifts
``session.id`` / ``user.id`` into canonical ``session_id`` / ``user_id``
per §5.2 (OTel GenAI) and §5.3 (OpenInference). :func:`prompt` tags
``hfao.prompt.{name,version,label}`` on the current span (§4.1).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from opentelemetry import baggage, trace
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.trace import Span, Tracer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider

    from hfao.config import HFAOConfig


_TRACER_NAME = "hfao.sdk"

# Baggage keys carried on the OTel context. We set both OpenInference
# (session.id / user.id) and OTel GenAI (gen_ai.conversation.id) so the
# normalizer resolves session_id regardless of which mapper it takes
# per §0 reader contract (OpenInference wins on collision).
_BAGGAGE_SESSION_ID = "session.id"
_BAGGAGE_USER_ID = "user.id"
_BAGGAGE_CONVERSATION_ID = "gen_ai.conversation.id"
_PROPAGATED_BAGGAGE_KEYS: tuple[str, ...] = (
    _BAGGAGE_SESSION_ID,
    _BAGGAGE_USER_ID,
    _BAGGAGE_CONVERSATION_ID,
)


class _SessionState:
    __slots__ = ("session_id", "user_id")

    def __init__(self, *, session_id: str | None, user_id: str | None) -> None:
        self.session_id = session_id
        self.user_id = user_id


_current_session: ContextVar[_SessionState | None] = ContextVar(
    "hfao_current_session", default=None
)


class HFAOBaggageSpanProcessor(SpanProcessor):
    """Copy HFAO session baggage onto every starting span.

    Registered by ``hfao.init()`` so descendants of a :func:`session`
    block — including spans produced by auto-instrumented OpenInference
    packages — carry the session attributes the normalizer expects.
    """

    def on_start(self, span: SDKSpan, parent_context: Context | None = None) -> None:
        for key in _PROPAGATED_BAGGAGE_KEYS:
            value = baggage.get_baggage(key, parent_context)
            if value is not None:
                span.set_attribute(key, str(value))

    def on_end(self, span: ReadableSpan) -> None:
        return

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


class HFAOContext:
    """Handle returned by :func:`hfao.init`.

    Holds the installed ``TracerProvider``, the resolved :class:`HFAOConfig`,
    and the list of auto-instrumented OpenInference packages.
    """

    __slots__ = ("config", "tracer_provider", "instrumentors", "mcp_patched", "_tracer")

    def __init__(
        self,
        *,
        config: HFAOConfig,
        tracer_provider: TracerProvider,
        instrumentors: tuple[str, ...],
        mcp_patched: bool,
    ) -> None:
        self.config = config
        self.tracer_provider = tracer_provider
        self.instrumentors = instrumentors
        self.mcp_patched = mcp_patched
        self._tracer = tracer_provider.get_tracer(_TRACER_NAME)

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    def session(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> _SessionContext:
        return _SessionContext(tracer=self._tracer, session_id=session_id, user_id=user_id)

    def prompt(
        self,
        name: str,
        *,
        version: int | None = None,
        label: str | None = None,
    ) -> None:
        _tag_current_span_with_prompt(name=name, version=version, label=label)


@contextmanager
def session(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
) -> Iterator[Span]:
    """Open a session block.

    Inside the ``with``, every span — including those produced by
    auto-instrumented frameworks — is stamped with ``session.id`` and
    ``user.id``. Nested sessions inherit the parent's values when kwargs
    are omitted.
    """
    from hfao.sdk.init import require_context

    ctx = require_context()
    inner = _SessionContext(tracer=ctx.tracer, session_id=session_id, user_id=user_id)
    with inner as span:
        yield span


def prompt(
    name: str,
    *,
    version: int | None = None,
    label: str | None = None,
) -> None:
    """Tag the current span with prompt metadata (§4.1 prompt_name/version/label)."""
    _tag_current_span_with_prompt(name=name, version=version, label=label)


def current_session() -> tuple[str | None, str | None]:
    """Return ``(session_id, user_id)`` for the innermost active session."""
    state = _current_session.get()
    if state is None:
        return (None, None)
    return (state.session_id, state.user_id)


class _SessionContext:
    __slots__ = (
        "_tracer",
        "_session_id",
        "_user_id",
        "_state_token",
        "_otel_token",
        "_cm",
    )

    def __init__(
        self,
        *,
        tracer: Tracer,
        session_id: str | None,
        user_id: str | None,
    ) -> None:
        self._tracer = tracer
        parent = _current_session.get()
        self._session_id = session_id or (parent.session_id if parent else None)
        self._user_id = user_id or (parent.user_id if parent else None)

    def __enter__(self) -> Span:
        ctx = otel_context.get_current()
        if self._session_id is not None:
            ctx = baggage.set_baggage(_BAGGAGE_SESSION_ID, self._session_id, context=ctx)
            ctx = baggage.set_baggage(_BAGGAGE_CONVERSATION_ID, self._session_id, context=ctx)
        if self._user_id is not None:
            ctx = baggage.set_baggage(_BAGGAGE_USER_ID, self._user_id, context=ctx)
        self._otel_token = otel_context.attach(ctx)

        state = _SessionState(session_id=self._session_id, user_id=self._user_id)
        self._state_token = _current_session.set(state)

        attrs: dict[str, str] = {}
        if self._session_id is not None:
            attrs[_BAGGAGE_SESSION_ID] = self._session_id
            attrs[_BAGGAGE_CONVERSATION_ID] = self._session_id
        if self._user_id is not None:
            attrs[_BAGGAGE_USER_ID] = self._user_id

        self._cm = self._tracer.start_as_current_span(
            name="hfao.session",
            attributes=attrs or None,
            kind=trace.SpanKind.INTERNAL,
        )
        return self._cm.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._cm.__exit__(exc_type, exc, tb)
        finally:
            _current_session.reset(self._state_token)
            otel_context.detach(self._otel_token)


def _tag_current_span_with_prompt(
    *,
    name: str,
    version: int | None,
    label: str | None,
) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    # SPEC §4.1 — the normalizer lifts hfao.prompt.* attributes into
    # Observation.prompt_name / prompt_version / prompt_label.
    span.set_attribute("hfao.prompt.name", name)
    if version is not None:
        span.set_attribute("hfao.prompt.version", version)
    if label is not None:
        span.set_attribute("hfao.prompt.label", label)


__all__ = [
    "HFAOContext",
    "HFAOBaggageSpanProcessor",
    "session",
    "prompt",
    "current_session",
]
