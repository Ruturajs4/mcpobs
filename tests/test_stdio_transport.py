"""The stdio transport's unique failure mode: stdout is the protocol.

Most MCP servers are launched by the CLIENT over stdio -- Claude Desktop, an
IDE, another agent -- and on that transport **stdout IS the JSON-RPC channel**.
A single stray `print()` anywhere in the import path or startup writes a line
into the middle of the protocol stream.

The codebase already knows this. There are three separate defensive comments
about it, and it has bitten once before: a diagnostic in `otel_bootstrap` printed
to stdout and stayed silent for weeks because only its failure branch printed.

WHAT WAS MISSING WAS A TEST. Measured before writing this: injecting
`print("[debug] seeding database")` into the demo server's startup corrupted the
channel -- the client logged `Invalid JSON: expected value at line 1 column 2,
input_value='[debug] seeding database'` -- and yet `python -m demo_server.
scenarios stdio` exited **0** with every scenario reporting `isError=False`.
The MCP client skips the unparseable line and carries on, so the corruption is
survivable, invisible to the demo, and entirely dependent on the client being
forgiving. A stricter client drops the connection.

So these tests assert on the BYTES, not on whether calls happen to succeed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _run_server_briefly(
    tmp_spans: Path, env_extra: dict[str, str] | None = None
) -> tuple[bytes, bytes]:
    """Start the demo server on stdio, send one request, capture both streams.

    Sends a real `initialize` so startup runs to completion -- seeding the
    databases, starting the partner APIs and installing instrumentation, which
    is exactly where a stray print would live.
    """
    import os

    # OTEL_MODE=file, NEVER the default `otlp`. Without it this test exported
    # its spans into the running local stack -- caught when `make verify` began
    # reporting a protocol version no scenario uses, traced back to these
    # subprocesses. A unit test must not write into the telemetry the acceptance
    # suite then asserts over.
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "OTEL_MODE": "file",
        "OTEL_SPAN_FILE": str(tmp_spans),
        **(env_extra or {}),
    }
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "stdout-purity-test", "version": "1.0"},
                },
            }
        )
        + "\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "demo_server.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        # communicate(), never a bare wait() on a PIPE: an undrained pipe blocks
        # the child at roughly 64KB, which is its own long-standing trap here.
        out, err = proc.communicate(request.encode(), timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return out, err


class TestStdioStdoutIsProtocolOnly:
    """Every line on stdout must be JSON-RPC. Nothing else may appear there."""

    @staticmethod
    @pytest.fixture(scope="class")
    def streams(tmp_path_factory: pytest.TempPathFactory) -> tuple[bytes, bytes]:
        spans = tmp_path_factory.mktemp("spans") / "stdio.ndjson"
        return _run_server_briefly(spans)

    def test_the_server_starts_and_answers_on_stdio(
        self, streams: tuple[bytes, bytes]
    ) -> None:
        out, err = streams
        assert out.strip(), f"server produced no stdout at all; stderr:\n{err.decode()[-2000:]}"

    def test_every_stdout_line_is_valid_json(self, streams: tuple[bytes, bytes]) -> None:
        """The assertion that a stray print fails.

        Not "did the call succeed" -- the MCP client skips a bad line and the
        call succeeds anyway, which is precisely why this went untested.
        """
        out, _ = streams
        offenders = []
        for index, raw in enumerate(out.decode("utf-8", "replace").splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                offenders.append((index, line[:120]))
        assert not offenders, (
            "non-JSON lines on stdout would corrupt the JSON-RPC stream:\n"
            + "\n".join(f"  line {i}: {text!r}" for i, text in offenders)
            + "\n\nOn stdio, stdout IS the protocol. Diagnostics belong on stderr."
        )

    def test_the_response_is_a_jsonrpc_envelope(
        self, streams: tuple[bytes, bytes]
    ) -> None:
        out, _ = streams
        messages = [
            json.loads(line)
            for line in out.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]
        assert messages, "no JSON-RPC messages on stdout"
        assert all(m.get("jsonrpc") == "2.0" for m in messages)
        assert any("result" in m or "error" in m for m in messages), (
            "no response to `initialize` -- the server started but did not answer"
        )

    def test_diagnostics_are_allowed_on_stderr(self, streams: tuple[bytes, bytes]) -> None:
        """The counterpart, so the rule reads as "use stderr", not "be silent".

        Instrumentation reports and seed failures SHOULD be visible. This
        asserts the channel they belong on still carries them, so nobody
        "fixes" a stdout violation by deleting the diagnostic.
        """
        _, err = streams
        assert err, (
            "no stderr at all -- the instrumentation report should appear there, "
            "and a silenced diagnostic is not the fix for a misdirected one"
        )
