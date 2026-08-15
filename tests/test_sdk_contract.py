"""THE TRIPWIRE.

The failure classifier reads SDK-generated message prefixes. Those are internal
formats that can change in any MCP SDK release, and if they do, every customer's
error intelligence silently degrades to a single bucket with nothing raising.

This test pins the contract against the *installed* SDK by driving a real
MCPServer in-process and asserting each failure mode still classifies correctly.
If an SDK upgrade breaks it, the build fails. That is the entire point -- do not
loosen these assertions to make them pass. Update the classifier, bump
CLASSIFIER_VERSION, and replay.

Runs in-process (the SDK's Client accepts an MCPServer directly), so it needs no
Docker, no Kafka and no ClickHouse.
"""

from __future__ import annotations

from typing import Any

from mcp.client import Client
from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent

from mcpobs import instrument
from mcpobs.classifier import FailureClassifier, FailureKind


def build_server() -> MCPServer:
    server = MCPServer("contract-test", version="0.0.1")

    @server.tool()
    def succeeds(message: str = "ok") -> str:
        """Clean success."""
        return message

    @server.tool()
    def returns_is_error(reason: str = "the tool said no") -> CallToolResult:
        """Tool ran and deliberately reported failure."""
        return CallToolResult(content=[TextContent(type="text", text=reason)], isError=True)

    @server.tool()
    def raises() -> str:
        """Handler raises -- the SDK wraps this before the span sees it."""
        raise RuntimeError("downstream credentials expired")

    @server.tool()
    def typed_argument(count: int = 1) -> str:
        """Used to trigger schema validation failure."""
        return str(count)

    return instrument(server)


async def call(tool: str, args: dict | None = None):
    """Drive one tool call in-process.

    The Client is opened and closed inside a single task deliberately. A
    pytest-asyncio async *fixture* yielding a live Client enters and exits the
    anyio cancel scope in different tasks, which fails with "Attempted to exit
    cancel scope in a different task than it was entered in".
    """
    async with Client(build_server()) as client:
        return await client.call_tool(tool, args or {})


class TestSdkMessageContract:
    """Each failure mode must still be distinguishable from the result text."""

    def setup_method(self) -> None:
        self.classifier = FailureClassifier()

    async def test_success_is_not_an_error(self) -> None:
        result = await call("succeeds", {"message": "hi"})
        assert not getattr(result, "is_error", False)

    async def test_tool_returned_is_error(self) -> None:
        result = await call("returns_is_error", {"reason": "upstream refused"})
        assert result.is_error
        # No SDK boilerplate: the text is the tool's own message.
        assert self.classifier.classify_result(result) == FailureKind.TOOL_ERROR

    async def test_raised_exception_is_distinguishable(self) -> None:
        """The case D13 said was lost. It is recoverable from the content."""
        result = await call("raises")
        assert result.is_error
        text = self.classifier.first_text(result)
        assert text.startswith(FailureClassifier.EXECUTION_PREFIX), (
            f"SDK changed its exception wrapper: {text[:120]!r}"
        )
        assert self.classifier.classify_result(result) == FailureKind.SERVER_EXCEPTION

    async def test_unknown_tool_is_distinguishable(self) -> None:
        result = await call("no_such_tool_exists")
        assert result.is_error
        text = self.classifier.first_text(result)
        assert text.startswith(FailureClassifier.UNKNOWN_TOOL_PREFIX), (
            f"SDK changed its unknown-tool message: {text[:120]!r}"
        )
        assert self.classifier.classify_result(result) == FailureKind.UNKNOWN_TOOL

    async def test_invalid_arguments_are_distinguishable(self) -> None:
        """Must not be misread as server_exception: the SDK wraps it in the
        same "Error executing tool " prefix, so ordering in classify() matters."""
        result = await call("typed_argument", {"count": {"not": "an int"}})
        assert result.is_error
        assert self.classifier.classify_result(result) == FailureKind.INVALID_ARGUMENTS

    async def test_all_four_kinds_are_mutually_distinct(self) -> None:
        """The regression that matters: a collapse back to one bucket."""
        kinds = set()
        for tool, args in [
            ("returns_is_error", {}),
            ("raises", {}),
            ("no_such_tool_exists", {}),
            ("typed_argument", {"count": {"bad": 1}}),
        ]:
            kinds.add(self.classifier.classify_result(await call(tool, args)))
        assert kinds == {
            FailureKind.TOOL_ERROR,
            FailureKind.SERVER_EXCEPTION,
            FailureKind.UNKNOWN_TOOL,
            FailureKind.INVALID_ARGUMENTS,
        }, f"failure kinds collapsed to {kinds}"


class TestSpanAnnotation:
    """The middleware must reach the SDK's still-open span."""

    async def test_span_carries_the_failure_kind(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        from opentelemetry import trace

        previous = trace.get_tracer_provider()
        trace._TRACER_PROVIDER = None
        trace.set_tracer_provider(provider)
        try:
            async with Client(build_server()) as client:
                await client.call_tool("raises", {})
            annotated = [
                s
                for s in exporter.get_finished_spans()
                if "mcpobs.failure.kind" in (s.attributes or {})
            ]
            assert annotated, "middleware did not reach the SDK's span"
            assert annotated[0].attributes["mcpobs.failure.kind"] == FailureKind.SERVER_EXCEPTION
        finally:
            trace._TRACER_PROVIDER = previous


class TestMrtrCorrelation:
    """MRTR round-trips do not share a trace_id; requestState is the only link."""

    def setup_method(self) -> None:
        self.classifier = FailureClassifier()

    def test_state_hash_is_stable(self) -> None:
        blob = '{"v":3,"outcomes":{},"asked":{"m:ask":"V65Cup8N"}}'
        assert self.classifier.mrtr_state(blob) == self.classifier.mrtr_state(blob)

    def test_different_state_gives_different_hash(self) -> None:
        assert self.classifier.mrtr_state("a") != self.classifier.mrtr_state("b")

    def test_hash_never_reveals_the_blob(self) -> None:
        """requestState carries recorded elicitation outcomes -- the user's
        answers. The hash must not leak them (D17/D28)."""
        secret = '{"outcomes":{"ask":{"approved":true,"note":"SENSITIVE"}}}'
        digest = self.classifier.mrtr_state(secret)
        assert "SENSITIVE" not in digest
        assert len(digest) == 16

    def test_missing_state_yields_empty(self) -> None:
        assert self.classifier.mrtr_state(None) == ""
        assert self.classifier.outgoing_state({}) == ""
        assert self.classifier.incoming_state(None) == ""

    def test_emitted_and_received_states_match(self) -> None:
        """The chaining property: round N's out == round N+1's in."""
        blob = '{"v":3,"asked":{"m:ask":"xyz"}}'
        assert self.classifier.outgoing_state({"requestState": blob}) == (
            self.classifier.incoming_state({"requestState": blob})
        )


class TestJsonRpcEnvelope:
    """Payloads are captured as the MCP wire message, not a fragment.

    The first version captured only `params["arguments"]` and the first text
    block, which is not a protocol message and threw away `_meta` entirely --
    including the ONLY place client identity appears.
    """

    def setup_method(self) -> None:
        from mcpobs.payload import PayloadCapture

        self.p = PayloadCapture()

    def test_request_is_a_jsonrpc_message(self) -> None:
        import json

        text, _ = self.p.request("tools/call", 7, {"name": "echo", "arguments": {"a": 1}})
        msg = json.loads(text)
        assert msg["jsonrpc"] == "2.0"
        assert msg["id"] == 7
        assert msg["method"] == "tools/call"
        assert msg["params"]["name"] == "echo"

    def test_meta_survives_because_it_carries_client_identity(self) -> None:
        """The SDK sets no client attribute on the span, so `_meta` is the only
        place `clientInfo` exists (V2 §6.1 asks which clients call which tools)."""
        import json

        text, _ = self.p.request("tools/call", 1, {
            "name": "echo",
            "_meta": {"io.modelcontextprotocol/clientInfo": {"name": "claude-code"}},
        })
        assert "claude-code" in json.loads(text)["params"]["_meta"][
            "io.modelcontextprotocol/clientInfo"]["name"]

    def test_request_state_is_redacted(self) -> None:
        """It carries recorded elicitation outcomes -- the user's answers (D28).
        Hashing it for correlation while dumping it verbatim here would be
        pointless."""
        text, _ = self.p.request("tools/call", 1, {
            "name": "confirm", "requestState": '{"outcomes":{"ask":{"approved":true}}}',
        })
        assert "approved" not in text
        assert "[redacted]" in text

    def test_response_keeps_structured_content(self) -> None:
        import json

        text, _ = self.p.response(3, {
            "content": [{"type": "text", "text": "hi"}],
            "isError": False, "resultType": "complete",
            "structuredContent": {"result": "hi"},
        })
        result = json.loads(text)["result"]
        assert result["structuredContent"] == {"result": "hi"}
        assert result["resultType"] == "complete"

    def test_works_for_non_tool_methods(self) -> None:
        """The first version looked for `content` blocks, so prompts/get
        (`messages`) and resources/read (`contents`) captured nothing."""
        import json

        text, _ = self.p.response(9, {"messages": [{"role": "user", "content": "x"}]})
        assert json.loads(text)["result"]["messages"]

    def test_binary_blocks_are_summarised_not_dumped(self) -> None:
        text, _ = self.p.response(1, {
            "content": [{"type": "image", "data": "iVBORw0KGgo" * 500, "mimeType": "image/png"}],
        })
        assert "iVBORw0KGgo" not in text
        assert "image omitted" in text


class RecordingSpan:
    """A span that remembers what was set on it, instead of a mock.

    The assertions are about WHICH attributes appear, so a real dict of
    attributes is both simpler to read and harder to fool than a mock whose
    call list has to be decoded.
    """

    def __init__(self) -> None:
        self.recorded: dict[str, object] = {}

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, key: str, value: object) -> None:
        self.recorded[key] = value


class TestClientIdentity:
    """`clientInfo` is promoted to a span attribute, not left in the payload.

    V2 §6.1 asks which clients call which tools. The SDK sets no client
    attribute, and before this the answer lived only inside a captured payload
    -- so the question could only be answered by customers who had also switched
    on the far more invasive payload capture. Two low-cardinality strings should
    not cost that.
    """

    def setup_method(self) -> None:
        from mcpobs.classifier import FailureClassifier

        self.c = FailureClassifier()

    def test_reads_name_and_version_from_meta(self) -> None:
        assert self.c.client_info({
            "_meta": {"io.modelcontextprotocol/clientInfo": {
                "name": "claude-code", "version": "2.1.0"}},
        }) == ("claude-code", "2.1.0")

    def test_absent_meta_is_empty_not_an_error(self) -> None:
        """Every shape a malformed request can take yields ("", ""). This runs on
        the customer's hot path; raising here would break their tool call."""
        for params in (None, {}, "nonsense", {"_meta": None}, {"_meta": {}},
                       {"_meta": {"io.modelcontextprotocol/clientInfo": "no"}},
                       {"_meta": {"io.modelcontextprotocol/clientInfo": {"name": 7}}}):
            assert self.c.client_info(params) == ("", "") or \
                self.c.client_info(params)[0] == ""

    def test_middleware_sets_the_attributes(self) -> None:
        from mcpobs.classifier import CLIENT_NAME_ATTRIBUTE, CLIENT_VERSION_ATTRIBUTE
        from mcpobs.middleware import FailureClassifierMiddleware

        span = RecordingSpan()
        recorded = span.recorded

        mw = FailureClassifierMiddleware()
        import mcpobs.middleware as mod

        original = mod.get_current_span
        mod.get_current_span = lambda: span  # type: ignore[assignment]
        try:
            mw._annotate(
                {"content": [{"type": "text", "text": "ok"}], "isError": False},
                {"_meta": {"io.modelcontextprotocol/clientInfo": {
                    "name": "inspector", "version": "0.9"}}},
            )
        finally:
            mod.get_current_span = original

        assert recorded[CLIENT_NAME_ATTRIBUTE] == "inspector"
        assert recorded[CLIENT_VERSION_ATTRIBUTE] == "0.9"

    def test_set_on_success_not_only_on_failure(self) -> None:
        """Client identity is a dimension, not an error detail. If it were only
        recorded on failures, "which clients call which tools" would answer with
        a breakdown of failures -- a subtly wrong answer, which is worse than
        none."""
        import inspect

        from mcpobs.middleware import FailureClassifierMiddleware

        source = inspect.getsource(FailureClassifierMiddleware._annotate)
        client_at = source.index("CLIENT_NAME_ATTRIBUTE")
        error_at = source.index("if not self.classifier.is_error(result):")
        assert client_at < error_at


class TestHttpBodyCapture:
    """Downstream HTTP capture (D60), against the hook contract as measured.

    The hooks do NOT receive `httpx.Request`/`httpx.Response`. They receive
    `RequestInfo(method, url, headers, stream, extensions)` and
    `ResponseInfo(status_code, headers, stream, extensions)` at the TRANSPORT
    layer. These fakes copy that shape deliberately: a test built on the
    convenient object rather than the real one passes while production does
    nothing, which is how the first cut of this file went green while capturing
    nothing at all.
    """

    def setup_method(self) -> None:
        from mcpobs.http import HttpBodyCapture

        self.h = HttpBodyCapture()
        self.span = RecordingSpan()
        self.recorded = self.span.recorded

    @staticmethod
    def _request(body: object = b"", headers: dict[str, str] | None = None) -> Any:
        class ByteStream:
            _stream = body

        class RequestInfo:
            stream = ByteStream()

        RequestInfo.headers = _Headers(headers or {})  # type: ignore[attr-defined]
        return RequestInfo()

    def test_records_the_request_body(self) -> None:
        from mcpobs.http import REQUEST_BODY_ATTRIBUTE

        self.h.on_request(self.span, self._request(b'{"q": "hello"}'))
        assert '"q": "hello"' in str(self.recorded[REQUEST_BODY_ATTRIBUTE])

    def test_a_streaming_upload_is_reported_not_drained(self) -> None:
        """`_stream` is an iterator for a streaming upload. Consuming it here
        would send an empty body -- the telemetry would have eaten the
        customer's data on its way out."""
        from mcpobs.http import REQUEST_BODY_ATTRIBUTE, STREAMING_REQUEST

        self.h.on_request(self.span, self._request(iter([b"chunk"])))
        assert self.recorded[REQUEST_BODY_ATTRIBUTE] == STREAMING_REQUEST

    def test_credential_headers_are_never_read(self) -> None:
        """Allow-list, not deny-list: anything unrecognised is dropped, so a
        bespoke `x-acme-session` is excluded without anyone predicting it."""
        from mcpobs.http import REQUEST_HEADERS_ATTRIBUTE

        self.h.on_request(self.span, self._request(b"", {
            "authorization": "Bearer supersecrettoken",
            "cookie": "session=abc",
            "x-acme-session": "nobody-predicted-this",
            "content-type": "application/json",
        }))
        rendered = str(self.recorded[REQUEST_HEADERS_ATTRIBUTE])
        assert "supersecrettoken" not in rendered
        assert "nobody-predicted-this" not in rendered
        assert "application/json" in rendered

    def test_bodies_are_redacted_like_every_other_payload(self) -> None:
        from mcpobs.http import REQUEST_BODY_ATTRIBUTE

        self.h.on_request(self.span, self._request(b'{"t": "sk-abcdefghijklmnopqrst"}'))
        assert "sk-abcdefghij" not in str(self.recorded[REQUEST_BODY_ATTRIBUTE])

    def test_binary_body_records_its_size_not_its_bytes(self) -> None:
        from mcpobs.http import REQUEST_BODY_ATTRIBUTE

        png = bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0xff, 0xfe])
        self.h.on_request(self.span, self._request(png))
        assert "binary" in str(self.recorded[REQUEST_BODY_ATTRIBUTE])

    def test_no_response_body_attribute_exists_at_all(self) -> None:
        """Not an oversight. The instrumentation's span ends when the transport
        returns and httpx reads the body afterwards, so any such attribute would
        be set on an ended span and silently dropped. An always-empty column
        reads as "this call had no response body", which is a wrong answer where
        we currently give none."""
        import mcpobs.http as mod

        assert not [n for n in dir(mod) if "RESPONSE_BODY" in n]

    def test_response_hook_still_records_headers(self) -> None:
        from mcpobs.http import RESPONSE_HEADERS_ATTRIBUTE

        class ResponseInfo:
            status_code = 200
            headers = _Headers({"content-type": "application/json", "server": "nginx"})

        self.h.on_response(self.span, None, ResponseInfo())
        assert "application/json" in str(self.recorded[RESPONSE_HEADERS_ATTRIBUTE])

    def test_async_hooks_exist_because_the_instrumentation_awaits_them(self) -> None:
        """`_handle_async_request_wrapper` does `await async_request_hook(...)`.
        A sync callable there raises inside the customer's HTTP call -- so the
        async variants are a correctness requirement, not a convenience."""
        import asyncio
        import inspect

        from mcpobs.http import REQUEST_BODY_ATTRIBUTE

        assert inspect.iscoroutinefunction(self.h.on_request_async)
        assert inspect.iscoroutinefunction(self.h.on_response_async)
        asyncio.run(self.h.on_request_async(self.span, self._request(b'{"a": 1}')))
        assert self.recorded[REQUEST_BODY_ATTRIBUTE]

    def test_a_broken_request_object_never_breaks_the_call(self) -> None:
        """The hook runs inside the customer's HTTP client. An exception here
        would fail their request, which no telemetry is worth."""

        class Exploding:
            @property
            def headers(self) -> dict[str, str]:
                raise RuntimeError("boom")

        self.h.on_request(self.span, Exploding())  # must not raise


class _Headers:
    """Stands in for `httpx.Headers`: case-insensitive, `.items()` of strings."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def items(self) -> Any:
        return self._values.items()
