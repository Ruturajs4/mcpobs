"""OTLP protobuf -> DecodedSpan.

The Collector's kafka exporter with `encoding: otlp_proto` writes serialized
ExportTraceServiceRequest messages. We keep the wire format all the way to here
and never transcode to JSON (Architecture.md §6.2).
"""

from __future__ import annotations

from typing import Any, Final

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Span, TracesData

from normalizer.models import DecodedSpan


class DecodeError(Exception):
    """Payload could not be parsed as OTLP -- routed to the DLQ."""


class OtlpDecoder:
    """Decodes one Kafka message into flattened spans."""

    SPAN_KIND: Final[dict[int, str]] = {
        0: "UNSPECIFIED",
        1: "INTERNAL",
        2: "SERVER",
        3: "CLIENT",
        4: "PRODUCER",
        5: "CONSUMER",
    }
    STATUS: Final[dict[int, str]] = {0: "UNSET", 1: "OK", 2: "ERROR"}

    def decode(self, payload: bytes) -> list[DecodedSpan]:
        message = self._parse(payload)
        spans: list[DecodedSpan] = []
        for resource_spans in message.resource_spans:
            resource_attributes = self._attributes(resource_spans.resource.attributes)
            for scope_spans in resource_spans.scope_spans:
                scope = scope_spans.scope.name if scope_spans.scope else ""
                for span in scope_spans.spans:
                    spans.append(self._flatten(span, resource_attributes, scope))
        return spans

    # -- internals ---------------------------------------------------------
    def _parse(self, payload: bytes) -> ExportTraceServiceRequest | TracesData:
        """Parse an OTLP payload, tolerating either top-level message shape."""
        for message_type in (ExportTraceServiceRequest, TracesData):
            try:
                message = message_type()
                message.ParseFromString(payload)
            except Exception:  # noqa: BLE001 - try the other shape
                continue
            if message.resource_spans:
                return message
        raise DecodeError("not a parseable OTLP payload with resource_spans")

    def _flatten(self, span: Span, resource_attributes: dict, scope: str) -> DecodedSpan:
        return DecodedSpan(
            trace_id=span.trace_id.hex(),
            span_id=span.span_id.hex(),
            parent_span_id=span.parent_span_id.hex(),
            span_name=span.name,
            span_kind=self.SPAN_KIND.get(span.kind, "UNSPECIFIED"),
            start_unix_nano=span.start_time_unix_nano,
            duration_ns=max(0, span.end_time_unix_nano - span.start_time_unix_nano),
            status_code=self.STATUS.get(span.status.code, "UNSET"),
            status_message=span.status.message or "",
            scope=scope,
            resource_attributes=resource_attributes,
            span_attributes=self._attributes(span.attributes),
            event_names=[event.name for event in span.events],
        )

    def _attributes(self, key_values: Any) -> dict[str, Any]:
        return {kv.key: self._any_value(kv.value) for kv in key_values}

    def _any_value(self, value: Any) -> Any:
        """Unwrap an OTLP AnyValue into a Python scalar."""
        which = value.WhichOneof("value")
        if which is None:
            return None
        if which == "array_value":
            return [self._any_value(v) for v in value.array_value.values]
        if which == "kvlist_value":
            return {kv.key: self._any_value(kv.value) for kv in value.kvlist_value.values}
        if which == "bytes_value":
            return value.bytes_value.hex()
        return getattr(value, which)
