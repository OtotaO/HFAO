"""HFAO SDK ``@observe`` decorator.

SPEC §12.1 rule 5. ``@hfao.observe`` turns any callable — sync, async,
generator, async generator — into an observation span with OpenInference
semantic conventions applied at the boundary:

- ``openinference.span.kind`` is set to the caller-requested kind
  (``CHAIN`` by default; pass ``type="TOOL"`` / ``"AGENT"`` / ``"RETRIEVER"`` /
  etc. to align with the canonical :class:`ObservationType` after §5.3
  mapping).
- ``input.value`` / ``output.value`` capture JSON-serialised args and
  return value (opt-out via ``capture_input=False`` /
  ``capture_output=False``).
- Exceptions are recorded and the span is marked ``ERROR`` before
  re-raising.

The decorator is safe to use before :func:`hfao.init` is called — the
OTel tracer returned in that case is a no-op, so the decorator adds no
observable overhead.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Literal, TypeVar, cast, overload

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

F = TypeVar("F", bound=Callable[..., Any])

# Caller-facing observation kinds. Mirrors SPEC §5.3 OpenInference mapping;
# the ingest normalizer turns these into the canonical ObservationType.
ObserveKind = Literal[
    "CHAIN",
    "TOOL",
    "AGENT",
    "RETRIEVER",
    "EMBEDDING",
    "LLM",
    "RERANKER",
    "EVALUATOR",
    "GUARDRAIL",
]

_TRACER_NAME = "hfao.sdk"


@overload
def observe(func: F, /) -> F: ...


@overload
def observe(
    *,
    name: str | None = None,
    type: ObserveKind = "CHAIN",  # noqa: A002 — spec §5.3 OpenInference field name
    capture_input: bool = True,
    capture_output: bool = True,
) -> Callable[[F], F]: ...


def observe(
    func: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    type: ObserveKind = "CHAIN",  # noqa: A002 — spec §5.3 OpenInference field name
    capture_input: bool = True,
    capture_output: bool = True,
) -> Any:
    """Wrap ``func`` in an observation span.

    Usable as ``@observe`` (bare) or ``@observe(name=..., type="TOOL")``.
    Sync, async, generator, and async-generator callables are each
    handled correctly; the span stays open for the lifetime of the
    iterator when a generator is returned.
    """

    def _decorate(fn: F) -> F:
        span_name = name or _qualified_name(fn)
        if inspect.isasyncgenfunction(fn):
            return cast("F", _wrap_async_gen(fn, span_name, type, capture_input, capture_output))
        if asyncio.iscoroutinefunction(fn):
            return cast("F", _wrap_async(fn, span_name, type, capture_input, capture_output))
        if inspect.isgeneratorfunction(fn):
            return cast("F", _wrap_gen(fn, span_name, type, capture_input, capture_output))
        return cast("F", _wrap_sync(fn, span_name, type, capture_input, capture_output))

    if func is not None:
        return _decorate(func)
    return _decorate


def _qualified_name(fn: Callable[..., Any]) -> str:
    mod = getattr(fn, "__module__", "")
    qn = getattr(fn, "__qualname__", getattr(fn, "__name__", "anonymous"))
    return f"{mod}.{qn}" if mod else qn


def _wrap_sync(
    fn: Callable[..., Any],
    span_name: str,
    kind: ObserveKind,
    capture_input: bool,
    capture_output: bool,
) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — wrapper boundary
        tracer = trace.get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(span_name) as span:
            _apply_entry_attrs(span, kind, capture_input, args, kwargs)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                _apply_error(span, exc)
                raise
            _apply_output(span, capture_output, result)
            return result

    return wrapper


def _wrap_async(
    fn: Callable[..., Awaitable[Any]],
    span_name: str,
    kind: ObserveKind,
    capture_input: bool,
    capture_output: bool,
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — wrapper boundary
        tracer = trace.get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(span_name) as span:
            _apply_entry_attrs(span, kind, capture_input, args, kwargs)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                _apply_error(span, exc)
                raise
            _apply_output(span, capture_output, result)
            return result

    return wrapper


def _wrap_gen(
    fn: Callable[..., Iterator[Any]],
    span_name: str,
    kind: ObserveKind,
    capture_input: bool,
    capture_output: bool,
) -> Callable[..., Iterator[Any]]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Iterator[Any]:  # noqa: ANN401 — wrapper boundary
        tracer = trace.get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(span_name) as span:
            _apply_entry_attrs(span, kind, capture_input, args, kwargs)
            collected: list[Any] = []
            try:
                for item in fn(*args, **kwargs):
                    if capture_output:
                        collected.append(item)
                    yield item
            except BaseException as exc:
                _apply_error(span, exc)
                raise
            if capture_output:
                _apply_output(span, True, collected)

    return wrapper


def _wrap_async_gen(
    fn: Callable[..., AsyncIterator[Any]],
    span_name: str,
    kind: ObserveKind,
    capture_input: bool,
    capture_output: bool,
) -> Callable[..., AsyncIterator[Any]]:
    @functools.wraps(fn)
    async def wrapper(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[Any]:  # noqa: ANN401 — wrapper boundary
        tracer = trace.get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(span_name) as span:
            _apply_entry_attrs(span, kind, capture_input, args, kwargs)
            collected: list[Any] = []
            try:
                async for item in fn(*args, **kwargs):
                    if capture_output:
                        collected.append(item)
                    yield item
            except BaseException as exc:
                _apply_error(span, exc)
                raise
            if capture_output:
                _apply_output(span, True, collected)

    return wrapper


def _apply_entry_attrs(
    span: trace.Span,
    kind: ObserveKind,
    capture_input: bool,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    span.set_attribute("openinference.span.kind", kind)
    if capture_input:
        span.set_attribute("input.value", _safe_json({"args": args, "kwargs": kwargs}))
        span.set_attribute("input.mime_type", "application/json")


def _apply_output(
    span: trace.Span,
    capture_output: bool,
    result: Any,
) -> None:
    if not capture_output:
        return
    span.set_attribute("output.value", _safe_json(result))
    span.set_attribute("output.mime_type", "application/json")


def _apply_error(span: trace.Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))


def _safe_json(value: Any) -> str:  # noqa: ANN401 — user payload boundary
    try:
        return json.dumps(value, default=_fallback, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(_fallback(value), ensure_ascii=False)


def _fallback(value: Any) -> str:  # noqa: ANN401 — user payload boundary
    try:
        return repr(value)
    except Exception:  # noqa: BLE001 — user __repr__ may raise
        return f"<unrepresentable {type(value).__name__}>"


__all__ = ["observe", "ObserveKind"]
