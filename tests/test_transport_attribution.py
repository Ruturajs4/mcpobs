"""The transport must be recorded, and must be right.

Before this, `network.transport` was empty on every stored span -- measured at
5,782 of 5,782 -- because the MCP SDK does not emit it. Nothing in the data
could distinguish a stdio server from a streamable-HTTP one.

WHY THAT MATTERED MORE THAN IT LOOKED. stdio is the common deployment: the
client launches the server, so most customer servers never bind a port. If
stdio silently stopped producing spans while HTTP kept working, no fleet-level
assertion could notice, because the two populations were indistinguishable once
stored. The gap hid exactly the transport most customers use.

These tests run the real server over the real transport and assert on what the
exporter actually emitted -- not on the detection function in isolation, which
would pass while the attribute never reached a span.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpobs import transport as t

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def _isolate() -> object:
    """Module state is process-wide; without this a test leaks into the next."""
    t.reset()
    yield
    t.reset()


class TestDetection:
    def test_run_records_the_transport_the_sdk_was_given(self) -> None:
        """The SDK's own value, not an inference from the environment."""
        calls = []

        class Server:
            middleware: list = []

            def run(self, transport="stdio", **kwargs):
                calls.append(transport)

        server = Server()
        t.observe_run(server)
        server.run("stdio")

        assert calls == ["stdio"], "the original run() must still be called"
        assert t.current() == "stdio"

    def test_a_bare_run_is_stdio(self) -> None:
        """`run()` with no argument IS stdio -- the SDK's own default.

        Leaving it blank would leave the most common deployment unattributed,
        which is the whole defect being fixed.
        """

        class Server:
            def run(self, transport="stdio", **kwargs):
                pass

        server = Server()
        t.observe_run(server)
        server.run()
        assert t.current() == "stdio"

    def test_explicit_beats_detection(self) -> None:
        t.set_transport("streamable-http", explicit=True)
        t.set_transport("stdio")  # a later guess must not overwrite it
        assert t.current() == "streamable-http"

    def test_an_unknown_value_is_ignored_rather_than_stored(self) -> None:
        """A dimension people group by must not be occasionally wrong."""
        t.set_transport("carrier-pigeon")
        assert t.current() is None

    def test_wrapping_twice_does_not_stack(self) -> None:
        class Server:
            depth = 0

            def run(self, transport="stdio", **kwargs):
                Server.depth += 1

        server = Server()
        t.observe_run(server)
        t.observe_run(server)
        server.run("stdio")
        assert Server.depth == 1


class TestEndToEnd:
    """The real server, over the real transport, asserting on exported spans.

    Not on `current()` -- that would pass even if the attribute never reached a
    span, which is precisely the bug. stdio is tested first and by name because
    it is the deployment most customers actually run.
    """

    @staticmethod
    def _capture(transport: str, tmp_path: Path) -> list[dict]:
        import asyncio

        from demo_server.scenarios import http_session, stdio_session

        span_file = tmp_path / f"spans-{transport}.ndjson"

        async def run() -> None:
            session = stdio_session if transport == "stdio" else http_session
            async with session(span_file=span_file) as client:
                await client.call_tool("echo_fast", {"message": "transport check"})
            await asyncio.sleep(1.5)  # let the subprocess flush and exit

        asyncio.run(run())
        if not span_file.exists():
            return []
        return [
            json.loads(line)
            for line in span_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _mcp_spans(spans: list[dict]) -> list[dict]:
        return [s for s in spans if "mcp.method.name" in (s.get("attributes") or {})]

    @pytest.mark.slow
    def test_stdio_spans_are_attributed_to_stdio(self, tmp_path: Path) -> None:
        spans = self._mcp_spans(self._capture("stdio", tmp_path))
        assert spans, "the stdio server exported no MCP spans at all"

        transports = {s["attributes"].get("network.transport") for s in spans}
        assert transports == {"stdio"}, (
            f"expected every stdio span to carry network.transport='stdio', got "
            f"{transports}. Empty means the attribute never reached the span."
        )

    @pytest.mark.slow
    def test_http_spans_are_attributed_to_streamable_http(self, tmp_path: Path) -> None:
        spans = self._mcp_spans(self._capture("http", tmp_path))
        assert spans, "the http server exported no MCP spans at all"

        transports = {s["attributes"].get("network.transport") for s in spans}
        assert transports == {"streamable-http"}, (
            f"expected streamable-http, got {transports}"
        )

    @pytest.mark.slow
    def test_the_two_transports_are_distinguishable(self, tmp_path: Path) -> None:
        """The point of the whole change.

        Before it, both populations stored an empty transport, so a stdio-only
        regression would hide behind healthy HTTP traffic.
        """
        stdio = self._mcp_spans(self._capture("stdio", tmp_path))
        http = self._mcp_spans(self._capture("http", tmp_path))

        stdio_values = {s["attributes"].get("network.transport") for s in stdio}
        http_values = {s["attributes"].get("network.transport") for s in http}

        assert stdio_values and http_values
        assert not (stdio_values & http_values), (
            "stdio and http spans are indistinguishable by transport: "
            f"{stdio_values} vs {http_values}"
        )
