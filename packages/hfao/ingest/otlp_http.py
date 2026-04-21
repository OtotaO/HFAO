"""OTLP/HTTP request parsing.

SPEC §5.1. Accepts both protobuf (``application/x-protobuf``) and JSON
(``application/json``) bodies for ``POST /v1/traces``. The result is a list
of flattened ``Span`` structs that ``ingest.normalize`` consumes.

The ``/v1/logs`` endpoint is also parsed here; log events whose name begins
with ``gen_ai.`` are pulled out as ``SpanEvent``s and returned alongside so
the server can fan them back onto their parent span.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, cast

from google.protobuf.json_format import Parse
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from hfao.schema.otlp import Span, SpanEvent, SpanStatus

_NS_PER_S = 1_000_000_000


def parse_traces(body: bytes, content_type: str) -> list[Span]:
    """Parse an OTLP/HTTP ``ExportTraceServiceRequest`` into flattened spans."""
    req = _parse_proto(body, content_type, trace_service_pb2.ExportTraceServiceRequest)
    out: list[Span] = []
    for rs in req.resource_spans:
        resource_attrs = _kv_to_dict(rs.resource.attributes) if rs.resource else {}
        for ss in rs.scope_spans:
            for sp in ss.spans:
                out.append(_span_from_proto(sp, resource_attrs))
    return out


def parse_logs(body: bytes, content_type: str) -> list[tuple[str, SpanEvent]]:
    """Parse an OTLP ``ExportLogsServiceRequest`` into (span_id_hex, event) pairs.

    Only ``gen_ai.*`` log record names are returned; other log records are
    ignored for v1 (§5.1).
    """
    req = _parse_proto(body, content_type, logs_service_pb2.ExportLogsServiceRequest)
    out: list[tuple[str, SpanEvent]] = []
    for rl in req.resource_logs:
        for sl in rl.scope_logs:
            for lr in sl.log_records:
                name = lr.event_name or _attr_str(lr.attributes, "event.name")
                if not name or not name.startswith("gen_ai."):
                    continue
                span_id = lr.span_id.hex()
                ts = datetime.fromtimestamp(
                    lr.time_unix_nano / _NS_PER_S, tz=timezone.utc
                )
                attrs = _kv_to_dict(lr.attributes)
                if lr.body.string_value:
                    attrs.setdefault("body", lr.body.string_value)
                out.append((span_id, SpanEvent(name=name, timestamp=ts, attributes=attrs)))
    return out


# ---- internals ----


def _parse_proto(body: bytes, content_type: str, msg_cls: Any) -> Any:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    msg = msg_cls()
    if ct in ("application/x-protobuf", "application/protobuf", ""):
        msg.ParseFromString(body)
    elif ct == "application/json":
        Parse(body.decode("utf-8"), msg)
    else:
        raise ValueError(f"Unsupported Content-Type: {content_type!r}")
    return msg


def _span_from_proto(
    sp: trace_pb2.Span, resource_attrs: dict[str, Any]
) -> Span:
    status_code_value = sp.status.code
    status: SpanStatus
    if status_code_value == trace_pb2.Status.STATUS_CODE_OK:
        status = "ok"
    elif status_code_value == trace_pb2.Status.STATUS_CODE_ERROR:
        status = "error"
    else:
        status = "unset"

    start = datetime.fromtimestamp(sp.start_time_unix_nano / _NS_PER_S, tz=timezone.utc)
    end = (
        datetime.fromtimestamp(sp.end_time_unix_nano / _NS_PER_S, tz=timezone.utc)
        if sp.end_time_unix_nano
        else None
    )

    events: list[SpanEvent] = []
    for ev in sp.events:
        ts = datetime.fromtimestamp(ev.time_unix_nano / _NS_PER_S, tz=timezone.utc)
        events.append(
            SpanEvent(
                name=ev.name,
                timestamp=ts,
                attributes=_kv_to_dict(ev.attributes),
            )
        )

    return Span(
        trace_id=sp.trace_id.hex(),
        span_id=sp.span_id.hex(),
        parent_span_id=sp.parent_span_id.hex() if sp.parent_span_id else None,
        name=sp.name or "(unnamed)",
        start_time=start,
        end_time=end,
        status=status,
        status_message=sp.status.message or None,
        attributes=_kv_to_dict(sp.attributes),
        resource_attributes=resource_attrs,
        events=events,
    )


def _kv_to_dict(kvs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in kvs:
        out[kv.key] = _any_value_to_python(kv.value)
    return out


def _any_value_to_python(v: common_pb2.AnyValue) -> Any:
    kind = v.WhichOneof("value")
    if kind == "string_value":
        return v.string_value
    if kind == "bool_value":
        return bool(v.bool_value)
    if kind == "int_value":
        return int(v.int_value)
    if kind == "double_value":
        return float(v.double_value)
    if kind == "array_value":
        return [_any_value_to_python(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _any_value_to_python(kv.value) for kv in v.kvlist_value.values}
    if kind == "bytes_value":
        return base64.b64encode(v.bytes_value).decode("ascii")
    return None


def _attr_str(kvs: Any, key: str) -> str | None:
    for kv in kvs:
        if kv.key == key:
            val = _any_value_to_python(kv.value)
            return val if isinstance(val, str) else json.dumps(val)
    return None


# Make the module's public surface tight.
_ = cast  # keep cast import usable from downstream without re-importing


__all__ = ["parse_traces", "parse_logs"]
