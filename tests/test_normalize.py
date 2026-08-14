"""Span -> row mapping, and the column/value alignment that used to be manual."""

from __future__ import annotations

from datetime import datetime

from normalizer.models import SpanRow
from normalizer.normalize import SpanNormalizer


class TestColumnAlignment:
    """The bug this whole model exists to prevent.

    Previously `to_row` returned a dict and COLUMNS was a hand-maintained
    parallel list. If they drifted, ClickHouse wrote values into the WRONG
    columns with no exception raised -- silent data corruption.
    """

    def test_values_align_with_columns(self, span_factory) -> None:
        row = SpanNormalizer().to_row(span_factory())
        columns, values = SpanRow.columns(), row.values()
        assert len(columns) == len(values)
        assert dict(zip(columns, values, strict=True))["mcp_tool_name"] == "echo_fast"

    def test_columns_derive_from_the_model(self) -> None:
        assert SpanRow.columns() == list(SpanRow.model_fields)

    def test_column_order_is_stable(self, span_factory) -> None:
        """Insert order depends on it; a reorder must be a deliberate change."""
        assert SpanRow.columns()[:5] == [
            "tenant_id",
            "project_id",
            "environment",
            "timestamp",
            "duration_ns",
        ]


class TestToRow:
    def setup_method(self) -> None:
        self.normalizer = SpanNormalizer()

    def test_promotes_observed_mcp_attributes(self, span_factory) -> None:
        row = self.normalizer.to_row(span_factory(), partition=3, offset=42)
        assert row.mcp_method == "tools/call"
        assert row.mcp_tool_name == "echo_fast"
        assert row.gen_ai_operation == "execute_tool"
        assert row.protocol_version == "2026-07-28"
        assert row.jsonrpc_request_id == "1"
        assert row.kafka_partition == 3
        assert row.kafka_offset == 42

    def test_service_identity_comes_from_resource_not_span(self, span_factory) -> None:
        row = self.normalizer.to_row(span_factory())
        assert row.service_name == "mcp-demo-server"
        assert row.service_version == "0.1.0"
        assert row.deployment_environment == "local"

    def test_unemitted_attributes_are_empty_not_missing(self, span_factory) -> None:
        row = self.normalizer.to_row(span_factory())
        assert row.mcp_session_id is None      # removed from the protocol
        assert row.rpc_status_code is None
        assert row.result_type == ""           # MRTR invisible (D11)
        assert row.transport == ""             # transport unobservable (D12)

    def test_rpc_status_code_stays_a_string(self, span_factory) -> None:
        """D14: the SDK sets str(code). Coercing to int would lose fidelity."""
        span = span_factory(span_attributes={"rpc.response.status_code": "-32602"})
        assert self.normalizer.to_row(span).rpc_status_code == "-32602"

    def test_raw_attributes_are_retained(self, span_factory) -> None:
        """Nothing we did not promote may be lost -- replay depends on it."""
        span = span_factory(span_attributes={"custom.vendor.key": "keep-me"})
        row = self.normalizer.to_row(span)
        assert row.span_attributes["custom.vendor.key"] == "keep-me"
        assert row.resource_attributes["service.name"] == "mcp-demo-server"

    def test_downstream_http_dimensions(self, span_factory) -> None:
        span = span_factory(
            span_attributes={
                "http.request.method": "GET",
                "http.response.status_code": 500,
                "server.address": "127.0.0.1",
            }
        )
        row = self.normalizer.to_row(span)
        assert (row.http_method, row.http_status_code, row.http_host) == ("GET", 500, "127.0.0.1")

    def test_legacy_http_semconv_names(self, span_factory) -> None:
        span = span_factory(
            span_attributes={"http.method": "POST", "http.status_code": "204"}
        )
        row = self.normalizer.to_row(span)
        assert (row.http_method, row.http_status_code) == ("POST", 204)

    def test_unparseable_http_status_does_not_raise(self, span_factory) -> None:
        span = span_factory(span_attributes={"http.status_code": "not-a-number"})
        assert self.normalizer.to_row(span).http_status_code is None

    def test_mcp_is_error_follows_the_taxonomy(self, span_factory) -> None:
        ok = self.normalizer.to_row(span_factory())
        failed = self.normalizer.to_row(
            span_factory(status_code="ERROR", span_attributes={"error.type": "tool_error"})
        )
        assert ok.mcp_is_error == 0
        assert failed.mcp_is_error == 1

    def test_arbitrary_method_names_pass_through(self, span_factory) -> None:
        """server/discover and subscriptions/listen are not in the OTel list."""
        span = span_factory(span_attributes={"mcp.method.name": "subscriptions/listen"})
        assert self.normalizer.to_row(span).mcp_method == "subscriptions/listen"

    def test_timestamp_converted_from_unix_nanos(self, span_factory) -> None:
        row = self.normalizer.to_row(span_factory(start_unix_nano=1_786_000_000_000_000_000))
        assert row.timestamp.year == 2026
        assert row.timestamp.tzinfo is None  # ClickHouse DateTime64 is naive UTC

    def test_sub_second_precision_is_not_lost_to_float_rounding(self, span_factory) -> None:
        """`unix_nano / 1e9` rounds: float64 has ~16 digits, epoch-ns needs 19."""
        row = self.normalizer.to_row(span_factory(start_unix_nano=1_786_000_000_123_456_789))
        assert row.timestamp.microsecond == 123_456  # truncated, not rounded to 123457

    def test_timestamp_is_utc_not_local(self, span_factory) -> None:
        """A naive local-time datetime would silently shift every span."""
        row = self.normalizer.to_row(span_factory(start_unix_nano=1_786_000_000_000_000_000))
        assert (row.timestamp - datetime(1970, 1, 1)).total_seconds() == 1_786_000_000
