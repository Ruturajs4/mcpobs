"""OTLP protobuf -> flat span dicts.

The Collector's kafka exporter with `encoding: otlp_proto` writes serialized
ExportTraceServiceRequest messages. We keep the wire format all the way to here
and never transcode to JSON (Architecture.md §6.2).
"""

from __future__ import annotations

from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Span, TracesData

_SPAN_KIND = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}


class DecodeError(Exception):
    """Payload could not be parsed as OTLP -- goes to the DLQ."""


def _any_value(value: Any) -> Any:
    """Unwrap an OTLP AnyValue into a Python scalar."""
    which = value.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_any_value(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _any_value(kv.value) for kv in value.kvlist_value.values}
    if which == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, which)


def _attrs(kvs: Any) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in kvs}


def parse(payload: bytes) -> ExportTraceServiceRequest | TracesData:
    """Parse an OTLP payload, tolerating either top-level message shape."""
    try:
        request = ExportTraceServiceRequest()
        request.ParseFromString(payload)
        if request.resource_spans:
            return request
    except Exception:  # noqa: BLE001 - fall through to the other shape
        pass
    try:
        data = TracesData()
        data.ParseFromString(payload)
        if data.resource_spans:
            return data
    except Exception as exc:
        raise DecodeError(f"not a parseable OTLP payload: {exc}") from exc
    raise DecodeError("OTLP payload contained no resource_spans")


def flatten(payload: bytes) -> list[dict[str, Any]]:
    """Decode one Kafka message into a list of flat span dicts."""
    message = parse(payload)
    out: list[dict[str, Any]] = []

    for resource_spans in message.resource_spans:
        resource_attributes = _attrs(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            scope = scope_spans.scope.name if scope_spans.scope else ""
            for span in scope_spans.spans:
                out.append(_flatten_span(span, resource_attributes, scope))
    return out


def _flatten_span(span: Span, resource_attributes: dict, scope: str) -> dict[str, Any]:
    span_attributes = _attrs(span.attributes)
    return {
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_span_id": span.parent_span_id.hex(),
        "span_name": span.name,
        "span_kind": _SPAN_KIND.get(span.kind, "UNSPECIFIED"),
        "start_unix_nano": span.start_time_unix_nano,
        "duration_ns": max(0, span.end_time_unix_nano - span.start_time_unix_nano),
        "status_code": _STATUS.get(span.status.code, "UNSET"),
        "status_message": span.status.message or "",
        "scope": scope,
        "resource_attributes": resource_attributes,
        "span_attributes": span_attributes,
        "event_names": [e.name for e in span.events],
    }
