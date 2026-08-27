"""Drive the demo server's tools over a given transport.

Every scenario is chosen to land in a distinct failure_category, so the Day-1
taxonomy is exercised against real SDK output rather than fixtures.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
HTTP_PORT = int(os.getenv("DEMO_HTTP_PORT", "8000"))
HTTP_URL = f"http://127.0.0.1:{HTTP_PORT}/mcp"

# (tool, arguments, expected failure_category)
SCENARIOS: list[tuple[str, dict, str]] = [
    ("echo_fast", {"message": "day-1"}, "ok"),
    ("fetch_status", {"code": 200}, "ok"),
    ("fetch_status", {"code": 500}, "ok"),
    ("soft_fail", {"reason": "upstream rejected the request"}, "tool_error"),
    ("explode", {}, "server_exception"),
    # Protocol-layer failures: these never reach a tool handler, so the SDK's
    # OpenTelemetryMiddleware sets rpc.response.status_code + a numeric error.type.
    ("no_such_tool", {}, "protocol_error"),
    ("echo_fast", {"message": {"not": "a string"}}, "protocol_error"),
    # MRTR: this client CAN answer (see `answer_elicitation`), so the call runs
    # to completion across two round-trips. The first round emits a real
    # `input_required` span -- the one that must never be counted as an error or
    # a latency sample (DF-1).
    ("confirm_deploy", {"service": "checkout"}, "pending_input"),
    # U6: downstream dimensions beyond HTTP.
    ("submit_order", {"customer": "acme", "sku": "widget-1"}, "ok"),
    ("enqueue_job", {"job": "reindex", "customer": "acme"}, "ok"),
    ("query_orders", {"customer": "acme"}, "ok"),
    ("summarize", {"text": "quarterly numbers"}, "ok"),
    # Real infrastructure: Postgres, MySQL, Redis and three local partner APIs.
    # Everything above this line reaches an in-process SQLite file or an
    # in-process HTTP server -- the easiest possible case, and not what a
    # customer's trace looks like.
    ("customer_profile", {"customer": "acme"}, "ok"),
    ("customer_profile", {"customer": "globex"}, "ok"),
    ("order_history", {"customer": "acme", "limit": 5}, "ok"),
    ("check_stock", {"sku": "widget-1"}, "ok"),
    ("cache_warm", {"customers": 5}, "ok"),
    ("partner_health", {}, "ok"),
    # The fan-out. Expected "ok", but the payments partner fails ~25% of the
    # time, so this row legitimately produces `server_exception` on some runs --
    # which is the point: the same tool, both outcomes, distinguishable only by
    # opening the trace.
    ("place_order", {"customer": "acme", "sku": "widget-1", "quantity": 1},
     "ok or server_exception"),
    ("place_order", {"customer": "globex", "sku": "gizmo-9", "quantity": 2},
     "ok or server_exception"),
]


#: Tools the demo calls OUTSIDE the `SCENARIOS` table. They belong to the
#: subscription flow rather than the failure taxonomy, so they are not scenarios
#: -- but assertion B6 derives its expected tool set from the demo rather than
#: hardcoding one, and it can only do that if the demo declares them.
SUBSCRIPTION_TOOLS: frozenset[str] = frozenset({"publish_change", "slow_export"})


async def answer_elicitation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Answer the server's question, so MRTR completes both round-trips.

    Without this the client lacks the elicitation capability and the server
    returns -32021 MissingRequiredClientCapability instead -- a protocol error,
    not an interim result, so the `input_required` path never gets exercised.
    """
    return {"action": "accept", "content": {"approved": True}}


async def run_capability_scenarios(client: Client) -> list[str]:
    """Exercise prompts and resources, not just tools.

    Produces `prompts/get`, `prompts/list`, `resources/read` and
    `resources/list` spans, so the console's prompt and resource views render
    real data rather than shipping empty.
    """
    out: list[str] = []
    for label, call in (
        ("prompts/list", lambda: client.list_prompts()),
        (
            "prompts/get triage_error",
            lambda: client.get_prompt("triage_error", {"tool": "explode"}),
        ),
        ("prompts/get summarize_incident", lambda: client.get_prompt("summarize_incident", {})),
        ("resources/list", lambda: client.list_resources()),
        ("resources/read config://limits", lambda: client.read_resource("config://limits")),
        ("resources/read docs://runbook/deploy", lambda: client.read_resource("docs://runbook/deploy")),
    ):
        try:
            await call()
            out.append(f"  {label:<52} -> ok")
        except Exception as exc:
            out.append(f"  {label:<52} -> raised {type(exc).__name__}")
    return out


async def run_scenarios(client: Client) -> list[str]:
    out: list[str] = []
    for tool, args, expected in SCENARIOS:
        label = f"{tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
        try:
            result = await client.call_tool(tool, args)
            is_error = bool(getattr(result, "is_error", False))
            out.append(f"  {label:<52} -> isError={is_error}  [expect {expected}]")
        except Exception as exc:
            out.append(f"  {label:<52} -> raised {type(exc).__name__}  [expect {expected}]")
    out += await run_capability_scenarios(client)
    out += await run_subscription_scenario(client)
    out += await run_progress_scenario(client)
    out += await run_cancellation_scenario(client)
    return out


async def run_cancellation_scenario(client: Client) -> list[str]:
    """Cancel a call in flight, by giving the client a deadline it will miss.

    Measured before this existed: the resulting span read as a fast SUCCESS --
    category `ok`, latency-eligible, duration truncated at the moment the client
    gave up. So a tool cancelled BECAUSE it is slow made the p95 look better.
    """
    out: list[str] = []
    try:
        await client.call_tool("slow_export", {"rows": 8}, read_timeout_seconds=0.05)
        out.append(f"  {'slow_export (cancelled)':<52} -> completed (NOT cancelled)")
    except Exception as exc:
        out.append(f"  {'slow_export (cancelled)':<52} -> {type(exc).__name__} as expected")
    return out


async def run_progress_scenario(client: Client) -> list[str]:
    """Call a long-running tool WITH a progress callback, and count the updates.

    The callback is the whole point. Progress notifications only flow when the
    client supplies a `progressToken`, so a client that does not ask receives
    none -- which means "we saw no progress" is ambiguous between "the server
    reported none" and "nobody asked". This asks.
    """
    received: list[str] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        received.append(f"{progress}/{total} {message or ''}".strip())

    out: list[str] = []
    try:
        await client.call_tool("slow_export", {"rows": 3}, progress_callback=on_progress)
        out.append(f"  {'slow_export (with progress_callback)':<52} -> {len(received)} updates")
        for update in received:
            out.append(f"  {'  progress: ' + update:<52} -> received")
    except Exception as exc:
        out.append(f"  {'slow_export':<52} -> {type(exc).__name__}: {exc}")
    return out


async def run_subscription_scenario(client: Client) -> list[str]:
    """Open a real `subscriptions/listen` stream, carry an event, close it.

    The point is the SPAN this produces. Its duration is a stream lifetime, and
    the rule that keeps such a span out of latency percentiles had been asserted
    since Day 2 against a synthetic span -- which tests the rule and not the
    pipeline. Assertion B8 can only be honest once a real one exists.

    Bounded by a timeout on purpose. A subscription that never yields would hang
    the scenario runner forever, and a demo that can hang is a demo nobody runs.
    """
    out: list[str] = []
    try:
        async with asyncio.timeout(20):
            async with client.listen(tools_list_changed=True) as subscription:
                out.append(f"  {'subscriptions/listen opened':<52} -> honored")
                await client.call_tool("publish_change", {"kind": "tools"})
                async for _event in subscription:
                    out.append(f"  {'  change event received':<52} -> ok")
                    break
    except TimeoutError:
        out.append(f"  {'subscriptions/listen':<52} -> TIMED OUT (no event arrived)")
    except Exception as exc:
        # Reported, never raised. A transport that cannot listen must not stop
        # the rest of the scenarios from producing telemetry.
        out.append(f"  {'subscriptions/listen':<52} -> {type(exc).__name__}: {exc}")
    return out


def _python() -> str:
    return sys.executable


@asynccontextmanager
async def stdio_session(span_file: Path | None = None) -> AsyncIterator[Client]:
    """Spawn the demo server as a stdio subprocess and connect to it."""
    # INHERIT the environment, as the HTTP path already does. `env=None` makes
    # the SDK hand the subprocess a minimal, sanitised environment, so
    # OTEL_EXPORTER_OTLP_* never reached it -- pointing the demo at the
    # authenticated gateway silently kept exporting to the old endpoint, and the
    # only symptom was an ingest log with no requests in it.
    env = dict(os.environ)
    if span_file:
        env["OTEL_MODE"] = "file"
        env["OTEL_SPAN_FILE"] = str(span_file)
    params = StdioServerParameters(
        command=_python(),
        args=["-m", "demo_server.server"],
        cwd=str(REPO_ROOT),
        env=env,
    )
    async with Client(
        stdio_client(params), elicitation_callback=answer_elicitation
    ) as client:
        yield client


async def run_auth_scenario(port: int) -> list[str]:
    """Hit an auth-enabled server with no token, a bad token, and a good one.

    Produces the spans DF-22 was filed for. None of this reaches an MCP method:
    a 401 is answered by the transport, so the HTTP span is the ONLY record the
    request happened -- which is exactly why the console showed nothing.
    """
    import httpx

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    out: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for label, headers in (
            ("no token", {}),
            ("bad token", {"authorization": "Bearer not-the-token"}),
            ("valid token", {"authorization": "Bearer demo-valid-token"}),
        ):
            try:
                r = await client.post(
                    f"http://127.0.0.1:{port}/mcp", json=body, headers=headers
                )
                out.append(f"  {'auth: ' + label:<52} -> HTTP {r.status_code}")
            except Exception as exc:
                out.append(f"  {'auth: ' + label:<52} -> {type(exc).__name__}")
    return out


def _wait_for_port(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


@asynccontextmanager
async def http_session(
    span_file: Path | None = None, port: int = HTTP_PORT
) -> AsyncIterator[Client]:
    """Spawn the demo server as a streamable-http service and connect to it."""
    env = dict(os.environ)
    if span_file:
        env["OTEL_MODE"] = "file"
        env["OTEL_SPAN_FILE"] = str(span_file)
    env["DOWNSTREAM_PORT"] = str(port + 899)

    # stdout is DISCARDED, not piped. A pipe nobody reads fills its ~64KB
    # buffer and then blocks the child forever -- before it ever binds its port,
    # so the symptom is "the server never started" rather than anything
    # mentioning a pipe. stderr stays piped because the error path below reads
    # it, and it is drained on every exit route.
    proc = subprocess.Popen(
        [_python(), "-m", "demo_server.server", "--http", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_for_port(port):
            err = proc.stderr.read().decode(errors="replace")[-2000:] if proc.stderr else ""
            raise RuntimeError(f"demo server did not open port {port}\n{err}")
        # mode="legacy": the installed mcp client's default "auto" mode probes
        # `server/discover` first and falls back to `initialize()` on any
        # error, but over streamable-HTTP it leaks the session ID the server
        # assigns to that FAILED probe into the fallback `initialize()` call
        # (mcp/client/streamable_http.py's _maybe_extract_session_id_from_response
        # stores the id from ANY response, error or not). The server then
        # rejects `initialize()` too, because a session ID that never
        # completed a handshake isn't a valid one to resume -- both calls
        # surface as `MCPError: Not Found`. "legacy" skips the probe and
        # calls `initialize()` directly, with no session ID yet to leak.
        # stdio_session() above is unaffected: it hands Client a Transport
        # instance directly, and stdio has no session-ID header to leak in
        # the first place.
        async with Client(
            f"http://127.0.0.1:{port}/mcp",
            elicitation_callback=answer_elicitation,
            mode="legacy",
        ) as client:
            yield client
        # Grace period: Windows terminate() is a hard kill with no flush, so we
        # let BatchSpanProcessor export on its own schedule first.
        await asyncio.sleep(float(os.getenv("DEMO_FLUSH_GRACE", "3")))
    finally:
        proc.terminate()
        try:
            # `communicate()` rather than `wait()`: it DRAINS stderr. `wait()`
            # on a process whose stderr pipe is full deadlocks -- the child
            # cannot exit until someone reads, and nobody is reading.
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else "both"

    if transport in ("stdio", "both"):
        print("stdio:")
        async with stdio_session() as client:
            for line in await run_scenarios(client):
                print(line)

    if transport in ("http", "both"):
        print("streamable-http:")
        async with http_session() as client:
            for line in await run_scenarios(client):
                print(line)


if __name__ == "__main__":
    asyncio.run(_main())
