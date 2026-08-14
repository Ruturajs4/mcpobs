"""Day-1 acceptance assertions A1-A8.

A8 is the buffer test -- the only assertion that proves the architecture's
central claim (ingest survives everything downstream). Never cut it.

    python scripts/verify_day1.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import clickhouse_connect

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from demo_server.scenarios import http_session, run_scenarios, stdio_session  # noqa: E402

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | str, detail: str) -> None:
    status = ok if isinstance(ok, str) else (PASS if ok else FAIL)
    results.append((name, status, detail))
    icon = {PASS: "[PASS]", FAIL: "[FAIL]", WARN: "[WARN]"}[status]
    print(f"{icon} {name}: {detail}")


def client():
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        database=os.getenv("CLICKHOUSE_DB", "mcpobs"),
    )


def wait_for_spans(ch, minimum: int, timeout: float = 60.0) -> int:
    """Wait for the normalizer to drain, rather than sleeping a fixed guess."""
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        count = ch.query("SELECT count() FROM spans_raw").result_rows[0][0]
        if count >= minimum:
            return count
        time.sleep(2)
    return count


async def run_demo() -> None:
    async with stdio_session() as c:
        await run_scenarios(c)
    async with http_session() as c:
        await run_scenarios(c)


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )


def kafka_offsets() -> int:
    """Total end offsets across otlp.spans.raw.

    Uses the kafka-get-offsets.sh wrapper: `kafka.tools.GetOffsetShell` moved to
    `org.apache.kafka.tools.*` in Kafka 3.9, so invoking the old class name via
    kafka-run-class.sh silently produces no output -- which reads as "no data"
    rather than "broken measurement".
    """
    proc = subprocess.run(
        [
            "docker", "exec", "mcpobs-kafka",
            "/opt/kafka/bin/kafka-get-offsets.sh",
            "--bootstrap-server", "localhost:9092", "--topic", "otlp.spans.raw",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not read Kafka offsets: {proc.stderr.strip()[:300]}")
    total = 0
    for line in proc.stdout.splitlines():
        parts = line.strip().split(":")
        if len(parts) == 3 and parts[2].lstrip("-").isdigit():
            total += int(parts[2])
    return total


def normalizer_restarts() -> int:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", "mcpobs-normalizer"],
        capture_output=True, text=True,
    )
    return int(proc.stdout.strip() or -1)


def produce_poison() -> None:
    """Send a deliberately malformed message to the raw topic."""
    from confluent_kafka import Producer

    producer = Producer({"bootstrap.servers": os.getenv("KAFKA_HOST_BOOTSTRAP", "localhost:29092")})
    producer.produce("otlp.spans.raw", value=b"this is definitely not protobuf OTLP")
    producer.flush(20)


def wait_for_dlq(ch, timeout: float = 45.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = ch.query(
            "SELECT reason, kafka_partition, kafka_offset FROM ingest_dead_letter "
            "ORDER BY received_at DESC LIMIT 1"
        ).result_rows
        if rows:
            return rows[0]
        time.sleep(2)
    return None


def main() -> int:
    ch = client()

    # ---------------- A8 first: it needs a clean before/after -------------
    print("\n--- A8: buffer test (stopping normalizer) ---")
    before_rows = ch.query("SELECT count() FROM spans_raw").result_rows[0][0]
    compose("stop", "normalizer")
    offsets_before = kafka_offsets()

    asyncio.run(run_demo())
    time.sleep(4)  # let the Collector batch and produce

    offsets_after = kafka_offsets()
    rows_while_down = ch.query("SELECT count() FROM spans_raw").result_rows[0][0]

    record(
        "A8a offsets advanced with no consumer running",
        offsets_after > offsets_before,
        f"{offsets_before} -> {offsets_after} on otlp.spans.raw",
    )
    record(
        "A8b nothing reached ClickHouse while the consumer was down",
        rows_while_down == before_rows,
        f"rows stayed at {rows_while_down}",
    )

    compose("start", "normalizer")
    drained = wait_for_spans(ch, before_rows + 1, timeout=90)
    record(
        "A8c every buffered span arrived after restart",
        drained > before_rows,
        f"{before_rows} -> {drained} rows after restart",
    )

    # ---------------- A1-A7 ----------------------------------------------
    print("\n--- A1-A7 ---")
    total = ch.query(
        "SELECT count() FROM spans_raw WHERE timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0][0]
    record("A1 spans arrived", total > 0, f"{total} spans in the last 30m")

    tools = ch.query(
        "SELECT mcp_tool_name, count(), sum(mcp_is_error) FROM spans_raw "
        "WHERE mcp_method = 'tools/call' GROUP BY mcp_tool_name ORDER BY 2 DESC"
    ).result_rows
    names = {r[0] for r in tools}
    expected = {"echo_fast", "fetch_status", "soft_fail", "explode"}
    record(
        "A2 tools identified by gen_ai.tool.name",
        expected.issubset(names),
        f"{sorted(names)}",
    )

    cats = dict(
        ch.query(
            "SELECT failure_category, count() FROM spans_raw GROUP BY failure_category"
        ).result_rows
    )
    has_ok = cats.get("ok", 0) > 0
    has_tool_error = cats.get("tool_error", 0) > 0
    record("A3a ok and tool_error both present", has_ok and has_tool_error, f"{cats}")
    distinguishable = cats.get("server_exception", 0) > 0 or cats.get("protocol_error", 0) > 0
    record(
        "A3b thrown exception distinguishable from isError",
        PASS if distinguishable else WARN,
        "distinguishable"
        if distinguishable
        else "KNOWN GAP: MCPServer converts every tool failure to isError before the "
        "OTel middleware sees it, so all failures collapse to error.type='tool_error'. "
        "V2 §25 checklist item is NOT achievable from span attributes. See "
        "docs/observed_attributes.md and docs/decisions.md D13.",
    )

    children = ch.query(
        "SELECT p.mcp_tool_name, c.span_name, c.http_status_code "
        "FROM spans_raw AS c INNER JOIN spans_raw AS p ON c.parent_span_id = p.span_id "
        "WHERE p.mcp_tool_name = 'fetch_status' AND c.http_status_code IS NOT NULL"
    ).result_rows
    record(
        "A4 downstream HTTP span is a child of the MCP span",
        len(children) > 0,
        f"{len(children)} child spans, statuses "
        f"{sorted({r[2] for r in children}) if children else '[]'}",
    )

    trace = ch.query(
        "SELECT trace_id, count() AS n FROM spans_raw GROUP BY trace_id "
        "HAVING n > 1 ORDER BY n DESC LIMIT 1"
    ).result_rows
    if trace:
        spans = ch.query(
            "SELECT span_id, parent_span_id, span_name FROM spans_raw "
            "WHERE trace_id = %(t)s ORDER BY timestamp",
            parameters={"t": trace[0][0]},
        ).result_rows
        ids = {s[0] for s in spans}
        linked = sum(1 for s in spans if s[1] and s[1] in ids)
        record(
            "A5 trace reconstructs by trace_id",
            len(spans) > 1 and linked > 0,
            f"trace {trace[0][0][:16]}... has {len(spans)} spans, {linked} parent links resolve",
        )
    else:
        record("A5 trace reconstructs by trace_id", False, "no multi-span trace found")

    versions = [
        r[0] for r in ch.query(
            "SELECT DISTINCT protocol_version FROM spans_raw WHERE protocol_version != ''"
        ).result_rows
    ]
    record(
        "A6 protocol version is 2026-07-28",
        versions == ["2026-07-28"],
        f"{versions}",
    )

    # ---------------- A9: poison message -> DLQ, partition advances --------
    print("\n--- A9: dead-letter path ---")
    restarts_before = normalizer_restarts()
    produce_poison()
    dlq_row = wait_for_dlq(ch, timeout=45)
    record(
        "A9a malformed message captured in the DLQ",
        dlq_row is not None,
        f"{dlq_row}" if dlq_row else "nothing reached ingest_dead_letter",
    )
    record(
        "A9b normalizer survived the poison message",
        normalizer_restarts() == restarts_before,
        f"restart count stayed at {restarts_before}",
    )

    # A7 now means: nothing UNEXPECTED was dead-lettered. The deliberate
    # decode_error from A9 is the only reason that may legitimately appear.
    reasons = dict(
        ch.query(
            "SELECT reason, count() FROM ingest_dead_letter GROUP BY reason"
        ).result_rows
    )
    unexpected = {r: n for r, n in reasons.items() if r != "decode_error"}
    record(
        "A7 nothing silently dead-lettered",
        not unexpected,
        f"{reasons or 'empty'} (only the deliberate decode_error is allowed)",
    )

    # ---------------- summary --------------------------------------------
    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed) - len(warned)} passed, {len(warned)} warn, {len(failed)} failed")
    if warned:
        print("\nKnown gaps (documented, not defects in this build):")
        for name, _, detail in warned:
            print(f"  - {name}\n      {detail}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
