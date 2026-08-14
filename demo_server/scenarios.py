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
]


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
    return out


def _python() -> str:
    return sys.executable


@asynccontextmanager
async def stdio_session(span_file: Path | None = None) -> AsyncIterator[Client]:
    """Spawn the demo server as a stdio subprocess and connect to it."""
    env = {"OTEL_MODE": "file", "OTEL_SPAN_FILE": str(span_file)} if span_file else {}
    params = StdioServerParameters(
        command=_python(),
        args=["-m", "demo_server.server"],
        cwd=str(REPO_ROOT),
        env=env or None,
    )
    async with Client(stdio_client(params)) as client:
        yield client


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

    proc = subprocess.Popen(
        [_python(), "-m", "demo_server.server", "--http", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_for_port(port):
            err = proc.stderr.read().decode(errors="replace")[-2000:] if proc.stderr else ""
            raise RuntimeError(f"demo server did not open port {port}\n{err}")
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            yield client
        # Grace period: Windows terminate() is a hard kill with no flush, so we
        # let BatchSpanProcessor export on its own schedule first.
        await asyncio.sleep(float(os.getenv("DEMO_FLUSH_GRACE", "3")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
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
