"""Demo MCP server (Python MCP SDK v2) used to generate real MCP telemetry.

Four tools, each chosen to produce a distinct failure_category so the Day-1
taxonomy has real data rather than synthetic data:

    echo_fast      -> ok          (baseline latency, clean success)
    fetch_status   -> ok          (+ downstream httpx child span, A4)
    soft_fail      -> tool_error  (isError=True, tool ran and said no)
    explode        -> tool_error  (handler raised -- see below)

NOTE: `explode` was written expecting `server_exception`, and it does NOT
produce one. MCPServer's `_handle_call_tool` catches the exception and converts
it to `CallToolResult(isError=True)` before `OpenTelemetryMiddleware` observes
the result, so the span is indistinguishable from `soft_fail`'s. That is the
D13 finding, and this tool is the evidence for it -- keep it. Day 2 recovers the
distinction from the result content without storing the content.

Run:
    python -m demo_server.server                 # stdio
    python -m demo_server.server --http          # streamable-http on :8000
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Annotated

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp_types import CallToolResult, TextContent
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from demo_server.otel_bootstrap import SERVICE_VERSION, init_telemetry, shutdown
from mcpobs import instrument

DOWNSTREAM_PORT = int(os.getenv("DOWNSTREAM_PORT", "8899"))
DOWNSTREAM_BASE = f"http://127.0.0.1:{DOWNSTREAM_PORT}"
DB_PATH = os.getenv("DEMO_DB_PATH", ":memory:")

mcp = MCPServer("mcp-demo-server", version=SERVICE_VERSION)

# The entire customer-facing integration. Annotates the SDK's existing span with
# a derived failure kind; creates no spans.
#
# `capture_payloads` is ON here and OFF in the library default: the demo exists
# to show what the product can do, and a real server should make that choice
# deliberately rather than inherit it.
instrument(mcp, capture_payloads=True)


# --------------------------------------------------------------------------
# A hermetic downstream HTTP service, so fetch_status needs no internet.
# --------------------------------------------------------------------------
class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        code = 200
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "status" and parts[1].isdigit():
            code = int(parts[1])
        body = f'{{"status": {code}}}'.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep stdio transport clean


def seed_database() -> None:
    """A tiny local table so query_orders has something real to query."""
    global DB_PATH
    DB_PATH = "file:demo?mode=memory&cache=shared"
    connection = sqlite3.connect(DB_PATH, uri=True)
    connection.execute("CREATE TABLE IF NOT EXISTS orders (customer TEXT)")
    connection.executemany(
        "INSERT INTO orders VALUES (?)", [("acme",), ("acme",), ("globex",)]
    )
    connection.commit()
    # Held open: an in-memory shared-cache database disappears when the last
    # connection to it closes.
    _KEEPALIVE.append(connection)


_KEEPALIVE: list[sqlite3.Connection] = []


def start_downstream() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", DOWNSTREAM_PORT), _StatusHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@mcp.tool()
def echo_fast(message: str = "hello") -> str:
    """Echo a message straight back. The clean success baseline."""
    return message


@mcp.tool()
async def fetch_status(code: int = 200) -> str:
    """Call a downstream HTTP endpoint that returns the given status code.

    Uses plain `httpx` (not the SDK's httpx2) so the OTel httpx instrumentor
    actually patches it and emits the downstream child span.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{DOWNSTREAM_BASE}/status/{code}")
        return f"downstream returned {response.status_code}"


@mcp.tool()
def soft_fail(reason: str = "upstream rejected the request") -> CallToolResult:
    """Return an MCP error result WITHOUT raising -> error.type == 'tool_error'."""
    return CallToolResult(
        content=[TextContent(type="text", text=reason)],
        isError=True,
    )


class Confirmation(BaseModel):
    approved: bool


def _ask_confirmation() -> Elicit[Confirmation]:
    """Resolver: the framework turns this into an InputRequiredResult.

    NOTE `ctx.elicit()` does NOT take this path -- it issues a legacy
    server-initiated `elicitation/create` over a back-channel, which a stateless
    2026-07-28 transport does not have (NoBackChannelError). Resolve/Elicit is
    the MRTR-native mechanism.
    """
    return Elicit("Deploy to production?", Confirmation)


async def confirm_deploy(
    confirmation: Annotated[Confirmation, Resolve(_ask_confirmation)],
    service: str = "api",
) -> str:
    """Must ask before acting -- produces a real MRTR `input_required` round.

    A client with no elicitation callback cannot answer, so the call stops at
    the interim result. That is exactly the span we need: `input_required` must
    be excluded from latency aggregates and never counted as an error.
    """
    return f"deployed {service}: {'ok' if confirmation.approved else 'declined'}"


mcp.tool()(confirm_deploy)


@mcp.tool()
def query_orders(customer: str = "acme") -> str:
    """Query a local database -- produces a real instrumented db child span.

    U6: proves `db_system` / `db_operation` promote, so a tool whose latency
    comes from a database shows WHERE the time went instead of looking like
    unexplained server time (V2 §6.1).

    MUST go through an explicit cursor. `opentelemetry-instrumentation-dbapi`
    wraps `Cursor.execute`, NOT the `Connection.execute` shortcut -- so the
    idiomatic one-liner produces NO span at all. Verified, not assumed. This is
    the kind of silent gap a customer reports as "my database calls do not show
    up", so it is worth knowing before they ask (D35).
    """
    connection = sqlite3.connect(DB_PATH, uri=True)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT count(*) FROM orders WHERE customer = ?", (customer,))
        return f"{cursor.fetchone()[0]} orders for {customer}"
    finally:
        connection.close()


@mcp.tool()
def summarize(text: str = "quarterly numbers") -> str:
    """A tool whose server calls an LLM downstream.

    The span below is what an instrumented LLM client emits -- we do NOT call a
    real model here, so the attributes are hand-set to the GenAI semantic
    convention. Labelled plainly rather than dressed up: it exercises our
    extraction, it does not prove any vendor's instrumentation.

    Note this is the SERVER calling a model, which we can see. What the client's
    model does is outside our boundary entirely (V2 §2.2).
    """
    tracer = trace.get_tracer("demo-llm")
    with tracer.start_as_current_span("chat gpt-4o-mini", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
        span.set_attribute("gen_ai.usage.input_tokens", 128)
        span.set_attribute("gen_ai.usage.output_tokens", 64)
        time.sleep(0.02)  # stand-in for model latency
    return f"summary of {text!r}"


# --------------------------------------------------------------------------
# Prompts and resources. The product covers all three MCP capability kinds
# (V2 §6.1); without these the prompts/resources views would ship having never
# rendered real data, and `prompts/get` / `resources/read` spans would not exist.
# --------------------------------------------------------------------------
@mcp.prompt()
def triage_error(tool: str = "fetch_status", message: str = "timeout") -> str:
    """Draft a triage note for a failing tool."""
    return f"Tool {tool} failed with: {message}. Suggest a first diagnostic step."


@mcp.prompt()
def summarize_incident(service: str = "checkout") -> str:
    """Summarise an incident for a given service."""
    return f"Summarise the last hour of incidents affecting {service}."


@mcp.resource("config://limits")
def resource_limits() -> str:
    """Static config the client can read."""
    return '{"max_concurrent": 8, "timeout_ms": 5000}'


@mcp.resource("docs://runbook/{name}")
def resource_runbook(name: str) -> str:
    """Templated resource -- exercises mcp.resource.uri with a real value."""
    return f"# Runbook: {name}\n\n1. Check downstream health\n2. Inspect recent traces"


@mcp.tool()
def explode() -> str:
    """Raise an unhandled exception.

    Yields error.type == 'tool_error', NOT 'RuntimeError': the SDK converts it
    before the span sees it (D13). The exception text survives only in the
    result content.
    """
    raise RuntimeError("deliberate failure: downstream credentials expired")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="run streamable-http instead of stdio")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    init_telemetry()
    start_downstream()
    seed_database()

    # BatchSpanProcessor buffers. Without an explicit flush the last spans of a
    # short-lived server are lost when the client tears the process down -- which
    # is exactly how stdio servers end. Flush on both normal exit and SIGTERM.
    def _flush_and_exit(*_: object) -> None:
        shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _flush_and_exit)
    atexit.register(shutdown)

    try:
        if args.http:
            mcp.run("streamable-http", host="127.0.0.1", port=args.port)
        else:
            mcp.run("stdio")
    finally:
        shutdown()


if __name__ == "__main__":
    main()
