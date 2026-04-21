"""Flattened OTLP span representation used by the ingest normalizer.

SPEC §5.6 talks in terms of ``ResourceSpan`` (the OTLP proto). We flatten
resource attributes onto each span at the HTTP layer so the normalizer can
stay pure — it only sees ``Span`` structs without having to walk the full
resource → scope → span hierarchy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from msgspec import Struct, field

SpanStatus = Literal["unset", "ok", "error"]


def _empty_attrs() -> dict[str, Any]:
    return cast("dict[str, Any]", {})


class SpanEvent(Struct, kw_only=True):
    """A single event recorded on an OTLP span (``gen_ai.*`` event names, etc.)."""

    name: str
    timestamp: datetime
    attributes: dict[str, Any] = field(default_factory=_empty_attrs)


def _empty_events() -> list[SpanEvent]:
    return cast("list[SpanEvent]", [])


class Span(Struct, kw_only=True):
    """Flattened OTLP span ready for normalization.

    ``attributes`` are the per-span key/values. ``resource_attributes`` is the
    parent ``Resource`` merged onto each span by the HTTP layer so the
    normalizer can look up ``hfao.project_id`` etc. without re-traversing
    the hierarchy.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    start_time: datetime
    end_time: datetime | None = None
    status: SpanStatus = "unset"
    status_message: str | None = None
    attributes: dict[str, Any] = field(default_factory=_empty_attrs)
    resource_attributes: dict[str, Any] = field(default_factory=_empty_attrs)
    events: list[SpanEvent] = field(default_factory=_empty_events)


__all__ = ["Span", "SpanEvent", "SpanStatus"]
