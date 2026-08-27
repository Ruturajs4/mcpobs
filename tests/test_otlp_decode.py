"""Decoder, against a real OTLP batch captured off the topic."""

from __future__ import annotations

import pytest

from normalizer.normalize import SpanNormalizer
from normalizer.otlp_decode import DecodeError, OtlpDecoder


class TestDecode:
    def setup_method(self) -> None:
        self.decoder = OtlpDecoder()

    def test_decodes_a_real_captured_batch(self, otlp_payload) -> None:
        spans = self.decoder.decode(otlp_payload)
        assert spans, "captured fixture should contain spans"

    def test_ids_are_hex_not_bytes(self, otlp_payload) -> None:
        span = self.decoder.decode(otlp_payload)[0]
        assert len(span.trace_id) == 32
        assert len(span.span_id) == 16
        int(span.trace_id, 16)  # raises if not hex

    def test_duration_is_never_negative(self, otlp_payload) -> None:
        for span in self.decoder.decode(otlp_payload):
            assert span.duration_ns >= 0

    def test_resource_attributes_are_present(self, otlp_payload) -> None:
        span = self.decoder.decode(otlp_payload)[0]
        assert span.resource_attributes.get("service.name") == "mcp-demo-server"

    def test_collector_stamped_tenant_survives_decode(self, otlp_payload) -> None:
        """The Collector's attributes/tenant processor is what we rely on."""
        span = self.decoder.decode(otlp_payload)[0]
        assert span.resource_attributes.get("tenant.id") == "local"

    def test_mcp_spans_carry_the_observed_attribute_set(self, otlp_payload) -> None:
        mcp_spans = [
            s for s in self.decoder.decode(otlp_payload) if "mcp.method.name" in s.span_attributes
        ]
        assert mcp_spans
        for span in mcp_spans:
            assert span.span_attributes["mcp.protocol.version"] == "2026-07-28"

    def test_end_to_end_decode_then_normalize(self, otlp_payload) -> None:
        normalizer = SpanNormalizer()
        rows = [normalizer.to_row(s) for s in self.decoder.decode(otlp_payload)]
        assert rows
        assert all(row.trace_id and row.span_id for row in rows)


class TestDecodeFailures:
    def setup_method(self) -> None:
        self.decoder = OtlpDecoder()

    @pytest.mark.parametrize(
        "payload",
        [
            b"this is definitely not protobuf OTLP",
            b"",
            b"\xff\xff\xff\xff",
        ],
    )
    def test_garbage_raises_decode_error(self, payload) -> None:
        """DecodeError is what routes a message to the DLQ instead of crashing."""
        with pytest.raises(DecodeError):
            self.decoder.decode(payload)

    def test_truncated_payload_raises_decode_error(self, otlp_payload) -> None:
        with pytest.raises(DecodeError):
            self.decoder.decode(otlp_payload[: len(otlp_payload) // 3])
