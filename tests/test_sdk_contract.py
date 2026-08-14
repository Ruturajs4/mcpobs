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
