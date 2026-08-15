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


class TestStatementParsing:
    """`db.operation` and `db.collection` derived from the statement (DF-12).

    The dbapi instrumentation emits neither, so "which table is slow" needed
    someone to read every statement by eye. Both are derivable from the
    statement we already store.
    """

    def parse(self, statement: str) -> tuple[str, str]:
        from normalizer.redact import parse_statement

        return parse_statement(statement)

    def test_select(self) -> None:
        assert self.parse("SELECT id, total FROM orders WHERE id = ?") == ("SELECT", "orders")

    def test_insert_and_update_and_delete(self) -> None:
        assert self.parse("INSERT INTO orders (id) VALUES (?)") == ("INSERT", "orders")
        assert self.parse("UPDATE orders SET total = ?") == ("UPDATE", "orders")
        assert self.parse("DELETE FROM orders WHERE id = ?") == ("DELETE", "orders")

    def test_case_and_whitespace_insensitive(self) -> None:
        assert self.parse("\n  select * from Orders\n") == ("SELECT", "Orders")

    def test_join_finds_the_driving_table_not_the_joined_one(self) -> None:
        op, table = self.parse("SELECT * FROM orders o JOIN users u ON u.id = o.user_id")
        assert (op, table) == ("SELECT", "orders")

    def test_quoted_and_schema_qualified_names(self) -> None:
        assert self.parse('SELECT * FROM "public"."orders"')[1].startswith("public")
        assert self.parse("SELECT * FROM shop.orders") == ("SELECT", "shop.orders")

    def test_unparseable_yields_nothing_never_a_guess(self) -> None:
        """A wrong label is worse than a missing one: it would silently
        misattribute latency to a table that was never queried. Anything this
        cannot parse returns what we had before -- empty."""
        for statement in ("", "   ", "PRAGMA foreign_keys", "EXPLAIN QUERY PLAN xyz"):
            assert self.parse(statement)[0] == ""

    def test_does_not_invent_a_table_for_a_tableless_statement(self) -> None:
        assert self.parse("SELECT 1")[1] == ""

    def test_parses_the_redacted_form_so_literals_do_not_leak_into_labels(self) -> None:
        """Redaction runs first in the normalizer. Verified here at the seam,
        because a derived label is exactly the kind of field that gets exported
        to a metrics backend where redaction no longer applies."""
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        span = DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={
                "db.system": "sqlite",
                "db.statement": "SELECT * FROM orders WHERE email = 'nick@example.com'",
            },
        )
        row = SpanNormalizer().to_row(span)
        assert row.db_operation == "SELECT"
        assert row.db_collection == "orders"
        assert "nick@example.com" not in row.db_statement

    def test_instrumentation_value_wins_over_the_derived_one(self) -> None:
        """Deriving is a fallback. If a future instrumentation library starts
        emitting `db.operation` it knows better than a regex, and this must not
        quietly override it."""
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        span = DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={
                "db.system": "sqlite",
                "db.operation": "BATCH",
                "db.collection.name": "authoritative",
                "db.statement": "SELECT * FROM orders",
            },
        )
        row = SpanNormalizer().to_row(span)
        assert (row.db_operation, row.db_collection) == ("BATCH", "authoritative")


class TestClientIdentityColumn:
    def test_client_attributes_become_columns(self) -> None:
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        span = DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={
                "mcp.method.name": "tools/call",
                "mcpobs.client.name": "claude-code",
                "mcpobs.client.version": "2.1.0",
            },
        )
        row = SpanNormalizer().to_row(span)
        assert (row.client_name, row.client_version) == ("claude-code", "2.1.0")

    def test_absent_client_is_empty_not_null(self) -> None:
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        row = SpanNormalizer().to_row(
            DecodedSpan(trace_id="a" * 32, span_id="b" * 16,
                        span_attributes={"mcp.method.name": "tools/list"})
        )
        assert row.client_name == ""


class TestHttpDownstreamColumns:
    def test_captured_http_detail_is_promoted_verbatim(self) -> None:
        """Not re-redacted here: it was already redacted and truncated in the
        customer's process, and running the scrubber twice over an already
        scrubbed string only risks mangling it."""
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        span = DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={
                "http.request.method": "GET",
                "mcpobs.http.request.body": '{"q": 1}',
                "mcpobs.http.request.headers": '{"content-type": "application/json"}',
                "mcpobs.http.response.headers": '{"content-type": "application/json"}',
            },
        )
        row = SpanNormalizer().to_row(span)
        assert row.http_request_body == '{"q": 1}'
        assert "content-type" in row.http_request_headers
        assert "content-type" in row.http_response_headers

    def test_there_is_no_response_body_column(self) -> None:
        """Deliberate, and worth pinning so nobody "fixes" the asymmetry back.

        The OTel client span ends when the transport returns; httpx reads the
        response body afterwards. A column for it could only ever be empty, and
        an always-empty column reads as "this call had no response body" --
        a wrong answer where we currently give none.
        """
        from normalizer.models import SpanRow

        assert "http_response_body" not in SpanRow.model_fields


class TestRealDriverAttributes:
    """Attribute shapes taken from REAL instrumentation, not invented.

    The postgres case below is a verbatim copy of what
    `opentelemetry-instrumentation-psycopg` emitted for an actual query against
    an actual Postgres. That distinction matters: a synthetic span written from
    the semconv spec passed happily while the real one was mis-parsed, because
    the spec is what SHOULD be emitted and this is what IS.
    """

    def row(self, attrs: dict) -> object:
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        return SpanNormalizer().to_row(
            DecodedSpan(trace_id="a" * 32, span_id="b" * 16, span_attributes=attrs)
        )

    POSTGRES = {
        "db.system": "postgresql",
        "db.name": "mcpobs_control",
        "db.statement": "SELECT slug, plan FROM orgs WHERE slug = %s",
        "db.user": "mcpobs",
        "net.peer.name": "localhost",
        "net.peer.port": 5433,
    }

    def test_db_name_is_not_treated_as_the_table(self) -> None:
        """`db.name` is the DATABASE -- renamed `db.namespace` in current
        semconv. Having it among the collection candidates made "which table is
        slow" answer `mcpobs_control` for every DBAPI driver, while the real
        table sat parsed and discarded. A wrong answer, not a missing one."""
        row = self.row(self.POSTGRES)
        assert row.db_collection == "orgs"
        assert row.db_collection != "mcpobs_control"

    def test_postgres_is_fully_attributed(self) -> None:
        row = self.row(self.POSTGRES)
        assert row.downstream_kind == "db"
        assert row.db_system == "postgresql"
        assert row.db_operation == "SELECT"

    def test_mysql_shares_the_dbapi_shape(self) -> None:
        """pymysql and psycopg both sit on the shared `dbapi` integration, so
        the attributes are the same and so is the handling."""
        row = self.row({
            "db.system": "mysql",
            "db.name": "shop",
            "db.statement": "UPDATE inventory SET qty = %s WHERE sku = %s",
        })
        assert (row.db_system, row.db_operation, row.db_collection) == (
            "mysql", "UPDATE", "inventory",
        )

    def test_the_database_name_is_still_recoverable(self) -> None:
        """Removed from the collection candidates, not thrown away. It is a
        different dimension, and an operator with several databases needs it."""
        row = self.row(self.POSTGRES)
        assert row.span_attributes["db.name"] == "mcpobs_control"

    def test_a_new_semconv_collection_still_wins(self) -> None:
        """When the instrumentation names the table properly, it knows better
        than a regex over the statement (D72)."""
        row = self.row({
            "db.system.name": "postgresql",
            "db.collection.name": "authoritative",
            "db.query.text": "SELECT * FROM something_else",
        })
        assert row.db_collection == "authoritative"


class TestCredentialRedaction:
    """No credential may reach the table, in ANY attribute.

    The spec makes this sharp: authorization "MUST be included in every HTTP
    request from client to server". So a bearer token is not an edge case in
    MCP-over-HTTP -- it is on every single request, and any instrumentation that
    captures request headers captures it.
    """

    def apply(self, attrs: dict) -> dict:
        from normalizer.redact import AttributeRedactor

        return AttributeRedactor().apply(attrs)

    def test_an_authorization_header_attribute_is_replaced_whole(self) -> None:
        """Not pattern-matched. The value may be an opaque token no shape
        matches -- a session cookie, a bespoke scheme -- so the KEY is the
        signal."""
        out = self.apply({
            "http.request.header.authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.abc.sig"
        })
        assert out["http.request.header.authorization"] == "[redacted]"

    def test_an_opaque_cookie_is_replaced_even_though_no_pattern_matches(self) -> None:
        out = self.apply({"http.response.header.set_cookie": "s=zzzzzzzzzzzzzzzzzzzz"})
        assert out["http.response.header.set_cookie"] == "[redacted]"

    def test_credential_shapes_are_scrubbed_in_unpredicted_keys(self) -> None:
        """THE regression. This used to scrub only six known keys, so a token in
        any other attribute was stored verbatim. Nothing had leaked only because
        nothing in the stack captured headers -- the protection was the absence
        of a feature, not a decision."""
        out = self.apply({
            "vendor.custom.thing": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            "some.log.line": "called with sk-abcdefghijklmnopqrstuvwx",
        })
        assert "Bearer eyJ" not in out["vendor.custom.thing"]
        assert "sk-abcdefghij" not in out["some.log.line"]

    def test_ordinary_attributes_are_untouched(self) -> None:
        """Over-redaction destroys debugging data as surely as under-redaction
        leaks (D113)."""
        attrs = {
            "gen_ai.tool.name": "echo_fast",
            "http.request.method": "GET",
            "mcp.method.name": "tools/call",
            "db.system": "postgresql",
        }
        assert self.apply(attrs) == attrs

    def test_short_values_skip_the_regex_pass(self) -> None:
        """The shortest credential shape is `Bearer ` plus eight characters, so
        anything under 15 chars cannot contain one. Most attributes are short,
        so this keeps a per-span regex cost off the normalizer's hot path."""
        assert self.apply({"x": "GET"})["x"] == "GET"

    def test_redaction_never_raises(self) -> None:
        """It runs on every span in the ingest path. An exception here would
        stop normalization for a whole batch."""
        assert self.apply({"a": None, "b": 42, "c": ""}) == {"a": None, "b": 42, "c": ""}
