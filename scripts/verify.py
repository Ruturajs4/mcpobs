"""Acceptance assertions. Cumulative across days -- Day 2 must not regress Day 1.

A8 is the buffer test -- the only assertion that proves the architecture's
central claim (ingest survives everything downstream). Never cut it.

    python scripts/verify_day1.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import clickhouse_connect

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from demo_server.scenarios import (  # noqa: E402
    SCENARIOS,
    SUBSCRIPTION_TOOLS,
    http_session,
    run_scenarios,
    stdio_session,
)

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


def consumer_lag() -> int:
    proc = subprocess.run(
        [
            "docker", "exec", "mcpobs-kafka",
            "/opt/kafka/bin/kafka-consumer-groups.sh",
            "--bootstrap-server", "localhost:9092", "--describe", "--group", "normalizer",
        ],
        capture_output=True, text=True,
    )
    total = 0
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[1] == "otlp.spans.raw" and fields[5].isdigit():
            total += int(fields[5])
    return total


def wait_for_zero_lag(timeout: float = 60.0) -> int:
    deadline = time.time() + timeout
    lag = -1
    while time.time() < deadline:
        lag = consumer_lag()
        if lag == 0:
            return 0
        time.sleep(3)
    return lag


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


def _not_a_failure_sql() -> str:
    """The taxonomy's non-failure categories, as a SQL list literal."""
    from normalizer.taxonomy import FailureTaxonomy

    return ", ".join(f"'{c}'" for c in FailureTaxonomy.NOT_A_FAILURE)


def _dev_key(name: str) -> str:
    """A local dev key from the gitignored file `make devkeys` writes.

    `make verify` depends on `devkeys`, so the file exists by the time this
    runs. Read rather than passed as an argument because the whole point of the
    F-series is to exercise the same path a customer uses, and a customer holds
    a key in a file too.
    """
    path = Path(__file__).resolve().parent.parent / ".mcpobs-keys.env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def main() -> int:
    ch = client()

    # ---------------- A8 first: it needs a clean before/after -------------
    print("\n--- A8: buffer test (stopping normalizer) ---")
    # Quiesce first. Taking the baseline while the normalizer is still draining
    # an earlier `make demo` makes A8b compare against a moving number, which
    # reads as a durability failure when it is only a race in this harness.
    print(f"  waiting for the pipeline to quiesce (lag={wait_for_zero_lag(timeout=90)})")
    compose("stop", "normalizer")
    time.sleep(3)  # let the container actually exit before sampling
    before_rows = ch.query("SELECT count() FROM spans_raw").result_rows[0][0]
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
    # V2 §25 launch gate. Was a documented WARN until U1: the raw span cannot
    # express this distinction, so the helper middleware recovers it from the
    # result content in the customer's process (D13/D17).
    record(
        "A3b thrown exception distinguishable from isError",
        cats.get("server_exception", 0) > 0 and cats.get("tool_error", 0) > 0,
        f"server_exception={cats.get('server_exception', 0)} vs "
        f"tool_error={cats.get('tool_error', 0)}",
    )

    # ---------------- B1-B3: the refined taxonomy -------------------------
    print("\n--- B1-B3: failure taxonomy v1 ---")
    expected_kinds = {"tool_error", "server_exception", "unknown_tool", "invalid_arguments"}
    present = {k for k in cats if k in expected_kinds and cats[k] > 0}
    record(
        "B1 all four failure kinds present on real telemetry",
        present == expected_kinds,
        f"{sorted(present)}" + (f" MISSING {sorted(expected_kinds - present)}" if
                                present != expected_kinds else ""),
    )

    # B2 originally asserted payload columns were NULL. That was the right
    # assertion while payload capture did not exist, but the wrong SHAPE: what
    # actually matters is that the taxonomy does not DEPEND on payloads, so that
    # a customer with capture off still gets precise failure classification.
    # Asserting emptiness would now just forbid a shipped feature.
    from mcpobs.middleware import FailureClassifierMiddleware

    record(
        "B2 payload capture is off by default",
        FailureClassifierMiddleware().capture_payloads is False,
        "instrument() does not send tool arguments or results unless asked",
    )

    classified_without_payload = ch.query(
        "SELECT count() FROM spans_raw "
        "WHERE failure_kind_source = 'helper' AND input_preview IS NULL "
        "  AND failure_category NOT IN ('', 'ok')"
    ).result_rows[0][0]
    record(
        "B2b the taxonomy works without payload capture",
        classified_without_payload > 0,
        f"{classified_without_payload} failures precisely classified with no payload stored",
    )

    # Scoped by EVENT time (`timestamp`), not ingest time. A replay reprocesses
    # history that predates the helper middleware, and those rows are correctly
    # span-sourced -- an attribute cannot be added retroactively to telemetry
    # produced before it existed. Crucially, `ingested_at` does NOT isolate the
    # current run either, because a replay re-ingests old spans *now*: replay
    # deliberately decouples the two clocks. For "is recent data healthy?",
    # event time is the only meaningful clock.
    # `protocol_error` is EXCLUDED, and that is not a loophole. An MCPError
    # propagates out of _handle_call_tool rather than being converted, so the
    # helper middleware never sees a result to classify -- but the raw span
    # already carries the numeric error.type, so span-sourced is CORRECT there.
    # The helper only matters where the SDK erased the distinction (D13).
    sources = dict(
        ch.query(
            "SELECT failure_kind_source, count() FROM spans_raw "
            # `pending_input` is excluded too: it is an MRTR interim result,
            # not a failure, and the helper deliberately returns before setting
            # a failure kind on it (D20). Including it here asserted that a
            # non-failure must be classified as one.
            # Excludes the taxonomy's OWN non-failures rather than a list
            # written by hand here. `cancelled` was added and B3 immediately
            # failed, because a hand-maintained copy of "what counts as a
            # failure" drifts from the definition the moment the definition
            # moves -- exactly the trap D57 called out.
            f"WHERE failure_category NOT IN ('', 'protocol_error', "
            f"                              {_not_a_failure_sql()}) "
            "  AND timestamp > now() - INTERVAL 15 MINUTE GROUP BY 1"
        ).result_rows
    )
    record(
        "B3 recent tool-level failures are classified by the helper",
        sources.get("helper", 0) > 0 and sources.get("span", 0) == 0,
        f"{sources} (span-sourced tool failures mean the helper is not attached)",
    )

    # D19 confirmed on real telemetry: Day 1 concluded protocol_error was
    # unreachable, but only because no demo tool triggered one.
    protocol = ch.query(
        "SELECT error_type, count() FROM spans_raw "
        "WHERE failure_category = 'protocol_error' "
        "  AND timestamp > now() - INTERVAL 15 MINUTE GROUP BY 1"
    ).result_rows
    # Asserts the SHAPE when protocol errors occur, not that they must occur.
    # They stopped appearing routinely once the demo client gained elicitation
    # capability (DF-1) -- which is a fix, not a regression. An assertion that
    # demands broken behaviour to stay green is worse than no assertion.
    record(
        "B3b protocol errors, when present, carry a JSON-RPC code (D19)",
        all(code.lstrip("-").isdigit() for code, _ in protocol),
        f"{[(code, n) for code, n in protocol]}" if protocol
        else "none in window -- the demo client now answers elicitations, so "
             "-32021 is no longer produced routinely",
    )

    # ---------------- B9: latency eligibility (U5) -------------------------
    print("\n--- B9: latency eligibility ---")
    ineligible = ch.query(
        "SELECT mcp_method, count() FROM spans_raw "
        "WHERE is_latency_eligible = 0 GROUP BY 1"
    ).result_rows
    # Counts BOTH sides. `wrongly_eligible == 0` was the whole assertion until
    # now, and it passed for days without a single subscription span existing --
    # a vacuous green, satisfied by the absence of the thing it claimed to
    # check. The demo now opens a real `subscriptions/listen` stream, so the
    # assertion can require the span to EXIST before congratulating itself on
    # its eligibility flag.
    total_streams, wrongly_eligible = ch.query(
        "SELECT count(), countIf(is_latency_eligible = 1) FROM spans_raw "
        "WHERE mcp_method LIKE 'subscriptions/listen%'"
    ).result_rows[0]
    record(
        "B9 real stream spans exist and none is latency-eligible",
        total_streams > 0 and wrongly_eligible == 0,
        f"{total_streams} subscriptions/listen span(s), {wrongly_eligible} wrongly eligible; "
        f"excluded so far: {ineligible or 'none seen yet'}"
        + ("  -- NO STREAM SPAN AT ALL, so this proved nothing" if not total_streams else ""),
    )

    # Cancellation. Measured before it was handled: a cancelled call landed as
    # category `ok`, latency-eligible, with its duration truncated at the moment
    # the client gave up -- so it inflated the success count AND deflated the
    # percentiles. A tool cancelled BECAUSE it was slow made the p95 look
    # better, which is the most misleading direction an error can run.
    cancelled = ch.query(
        "SELECT count(), countIf(is_latency_eligible = 1), countIf(mcp_is_error = 1) "
        "FROM spans_raw WHERE failure_category = 'cancelled'"
    ).result_rows[0]
    record(
        "B9b cancelled calls exist, are not errors, and are not latency samples",
        cancelled[0] > 0 and cancelled[1] == 0 and cancelled[2] == 0,
        f"{cancelled[0]} cancelled, {cancelled[1]} wrongly eligible, "
        f"{cancelled[2]} wrongly counted as errors"
        + ("  -- NO CANCELLED SPAN AT ALL, so this proved nothing" if not cancelled[0] else ""),
    )

    # ---------------- B4-B5: trace assembly (ADR-005) ---------------------
    print("\n--- B4-B5: trace assembly ---")
    # The synthetic idempotency probe is excluded from BOTH sides. It is not
    # product data, and comparing a filtered table against an unfiltered
    # aggregate compares two different populations.
    #
    # A related trap, learned by falling into it: a `DELETE FROM spans_raw`
    # removes rows from the source but NOT from a materialized view's target
    # table -- MVs fire on INSERT only. Deleting from a source table silently
    # desynchronises every aggregate built on it.
    # Both names: an earlier probe used 'verify' before it was renamed, and
    # those rows survive in trace_summaries even though they were deleted from
    # spans_raw -- which is precisely the desynchronisation described above.
    probe = "AND tenant_id = 'local'"
    raw_traces, summary_traces = ch.query(
        f"SELECT (SELECT uniqExact(trace_id) FROM spans_raw WHERE 1 {probe}), "
        f"       (SELECT uniqExact(trace_id) FROM trace_summaries WHERE 1 {probe})"
    ).result_rows[0]
    # The raw side counts DISTINCT SPANS, not rows. It used to count rows, and
    # passed -- because `trace_summaries` counted the same replayed spans twice
    # and the two errors cancelled. So this assertion was agreeing with a
    # double-count rather than checking a truth, which is the exact trap its own
    # comment above warns about: an aggregate compared against a different
    # population. Only visible once the recompute (DF-7) made one side correct.
    raw_spans, summed_spans = ch.query(
        f"SELECT (SELECT count() FROM ("
        f"           SELECT span_id FROM spans_raw WHERE 1 {probe} "
        f"           GROUP BY tenant_id, project_id, trace_id, span_id)), "
        f"       (SELECT sum(span_count) FROM trace_summaries WHERE 1 {probe})"
    ).result_rows[0]
    # Traces EXACTLY; spans at-least. That asymmetry is not a fudge -- it is the
    # difference between the two failure modes.
    #
    # A missing span means the aggregate lost data, which is a defect. An EXTRA
    # span means the materialized view counted a replayed one twice, which is
    # DF-7's known and documented MV limitation (D75) -- and this script replays
    # a span itself, in B11b, before reaching here. Demanding equality made B4
    # fail for the pipeline working exactly as designed.
    #
    # Exact reconciliation is asserted by E4, AFTER `make rollups` has run. What
    # B4 owns is its own claim: one row per trace.
    record(
        "B4 trace_summaries accounts for every distinct trace and span",
        raw_traces == summary_traces and summed_spans >= raw_spans,
        f"{summary_traces}/{raw_traces} traces, {summed_spans}/{raw_spans} spans accounted for"
        + (f" (+{summed_spans - raw_spans} from B11b's replay, reconciled by E4)"
           if summed_spans > raw_spans else ""),
    )

    # A trace assembled from more than one span is the case that matters: it
    # proves incremental aggregation across separate inserts, not just a
    # pass-through of single-span traces.
    multi = ch.query(
        "SELECT max(tool_name), sum(span_count), argMaxMerge(failure_category) "
        "FROM trace_summaries GROUP BY tenant_id, project_id, trace_id "
        "HAVING sum(span_count) > 1 ORDER BY 2 DESC LIMIT 1"
    ).result_rows
    record(
        "B4b multi-span traces assemble correctly",
        bool(multi) and multi[0][1] > 1,
        f"largest assembled trace: tool={multi[0][0]!r} spans={multi[0][1]} "
        f"category={multi[0][2]!r}" if multi else "no multi-span trace found",
    )

    # NOTE THE `LIMIT 1 BY`. trace_locator is a ReplacingMergeTree, and
    # ReplacingMergeTree deduplicates only when parts merge -- which is
    # asynchronous and may never have happened. A replay re-inserts every
    # trace_id, so a naive lookup returns the same trace several times. Any
    # locator read MUST dedupe explicitly; Day 3's API depends on this.
    located = ch.query(
        "SELECT l.tenant_id, l.project_id, l.trace_date FROM trace_locator AS l "
        "INNER JOIN (SELECT trace_id FROM spans_raw GROUP BY trace_id "
        "            HAVING count() > 1 LIMIT 1) AS t ON l.trace_id = t.trace_id "
        "ORDER BY l.first_seen DESC LIMIT 1 BY l.trace_id"
    ).result_rows
    record(
        "B5 trace_locator resolves a trace to exactly one tenant/project/date",
        len(located) == 1,
        f"{located}" if located else "locator did not resolve a known trace",
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
        "SELECT trace_id, count() AS n FROM spans_raw "
        "WHERE service_name != 'verify-probe' "
        "GROUP BY trace_id HAVING n > 1 ORDER BY n DESC LIMIT 1"
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

    # ---------------- B6: replay correctness (ADR-007) ---------------------
    # A normalizer bug must be recoverable by reprocessing from Kafka rather
    # than by asking customers to resend. That requires reads to resolve the
    # LATEST normalization_version per span -- a naive query mixing versions
    # returns the buggy rows alongside the corrected ones.
    print("\n--- B6: replay / normalization_version ---")
    resolved = {
        r[0]
        for r in ch.query(
            "SELECT mcp_tool_name FROM ("
            "  SELECT trace_id, span_id,"
            "         argMax(mcp_tool_name, normalization_version) AS mcp_tool_name,"
            "         argMax(mcp_method, normalization_version) AS mcp_method"
            "  FROM spans_raw GROUP BY trace_id, span_id"
            ") WHERE mcp_method = 'tools/call' GROUP BY mcp_tool_name"
        ).result_rows
    }
    # Derived from the scenarios, not hardcoded: a hardcoded list goes stale the
    # moment a demo tool is added, and a stale assertion is worse than none --
    # it fails for the wrong reason and trains you to ignore it.
    expected_tools = {tool for tool, _, _ in SCENARIOS} | SUBSCRIPTION_TOOLS
    record(
        "B6 version-resolved read returns the correct tool set",
        resolved == expected_tools,
        f"{sorted(resolved)}"
        + (f" UNEXPECTED {sorted(resolved - expected_tools)}" if resolved - expected_tools else ""),
    )

    versions = sorted(
        r[0] for r in ch.query(
            "SELECT DISTINCT normalization_version FROM spans_raw"
        ).result_rows
    )
    record(
        "B6b normalization_version is stamped on every row",
        all(v > 0 for v in versions),
        f"versions present: {versions}"
        + (" (multiple versions = a replay happened, which is the point)"
           if len(versions) > 1 else ""),
    )

    # ---------------- B8: duration data quality ---------------------------
    # A span can be no more precise than the clock OTel reads. On a coarse
    # clock, fast tool calls record as duration_ns = 0 and their latency
    # percentiles become meaningless -- silently, because a zero is a number
    # and charts happily plot it. Surfaced as a WARN, not a FAIL: it is an
    # environment property, not a defect in this build.
    print("\n--- B8: duration data quality ---")
    zero, total = ch.query(
        "SELECT countIf(duration_ns = 0), count() "
        "FROM spans_raw WHERE mcp_method = 'tools/call' "
        "  AND timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0]
    ratio = (zero / total) if total else 0.0
    nonzero_floor = ch.query(
        "SELECT min(duration_ns) FROM spans_raw WHERE duration_ns > 0 "
        "  AND timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0][0]
    record(
        "B8 tool-call durations are measurable",
        PASS if ratio < 0.10 else WARN,
        f"{zero}/{total} ({ratio:.0%}) zero-duration; smallest non-zero span "
        f"{(nonzero_floor or 0) / 1e6:.3f}ms"
        + (
            "  -- COARSE CLOCK. No longer a gap this script only tells US "
            "about: assertion E5b checks the console says so too (DF-4, D81)."
            if ratio >= 0.10
            else ""
        ),
    )

    # ---------------- B10: downstream dimensions (U6) ----------------------
    # V2 §6.1: a slow tool call must show WHERE the time went. Without this,
    # database and LLM latency both look like unexplained server time.
    print("\n--- B10: downstream dimensions ---")
    kinds = dict(
        ch.query(
            "SELECT downstream_kind, count() FROM spans_raw "
            "WHERE downstream_kind != '' AND timestamp > now() - INTERVAL 30 MINUTE GROUP BY 1"
        ).result_rows
    )
    record(
        "B10 http, db and llm downstream calls are all attributed",
        {"http", "db", "llm"}.issubset(kinds),
        f"{kinds}",
    )

    waterfall = ch.query(
        "SELECT p.mcp_tool_name, c.downstream_kind FROM spans_raw AS c "
        "INNER JOIN spans_raw AS p ON c.parent_span_id = p.span_id "
        "WHERE c.downstream_kind != '' AND p.mcp_tool_name != '' "
        "GROUP BY 1, 2"
    ).result_rows
    record(
        "B10b each downstream call is attributable to its tool",
        len({kind for _, kind in waterfall}) >= 3,
        f"{sorted({(tool, kind) for tool, kind in waterfall})}",
    )

    # ---------------- B11: insert idempotency (ADR-006) --------------------
    # Was untestable for two days because the local table was MergeTree, where
    # `insert_deduplication_token` is inert. The local stack now runs embedded
    # Keeper and ReplicatedMergeTree, so the real mechanism is exercised here
    # rather than deferred to an environment that does not exist.
    print("\n--- B11: insert idempotency ---")
    from normalizer.clickhouse_sink import ClickHouseSink
    from normalizer.models import SpanRow

    sink = ClickHouseSink()
    run_id = f"{int(time.time()):x}".rjust(16, "0")
    token = f"verify-idempotency-{run_id}"
    # A UNIQUE trace per run. Reusing one id made every run append another span
    # under the same (trace_id, span_id), which accumulated into a synthetic
    # multi-span trace that A5 then picked as its example and failed on.
    # Synthetic data must not be mistakable for product data.
    # Its OWN tenant, not `local`. Synthetic test data must be invisible to
    # product queries -- it was showing up as a second "server" in the console --
    # and putting it in a separate tenant exercises the scoping at the same time.
    probe = SpanRow(
        tenant_id="_verify",
        project_id="_verify",
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        trace_id=("f" * 16) + run_id,
        span_id=run_id,
        span_name="idempotency-probe",
        service_name="verify-probe",
    )

    def probe_count() -> int:
        return ch.query(
            "SELECT count() FROM spans_raw WHERE span_name = 'idempotency-probe' "
            "  AND trace_id = %(t)s",
            parameters={"t": probe.trace_id},
        ).result_rows[0][0]

    baseline = probe_count()
    sink.insert_spans([probe], dedup_token=token)
    sink.insert_spans([probe], dedup_token=token)  # identical block, same token
    time.sleep(2)
    after_same = probe_count() - baseline
    record(
        "B11 an identical batch replayed with the same token is deduplicated",
        after_same == 1,
        f"two inserts of one row produced {after_same} row(s) -- ReplicatedMergeTree "
        f"+ insert_deduplication_token",
    )

    sink.insert_spans([probe], dedup_token=f"{token}-different")
    time.sleep(2)
    after_different = probe_count() - baseline
    record(
        "B11b a DIFFERENT token inserts again, by design",
        after_different == 2,
        f"{after_different} rows. This is why a bug-fix replay produces new rows "
        f"rather than being silently discarded (D38)",
    )

    # ---------------- C1-C5: the query API (Day 3) -------------------------
    # The endpoints are asserted against the live service, not mocked: the
    # things most likely to be wrong -- tenant scoping, version resolution,
    # pending_input leaking into errors -- only manifest against real data.
    print("\n--- C1-C5: query API ---")
    import urllib.error
    import urllib.request

    read_key = _dev_key("MCPOBS_READ_KEY")

    def api(path: str, key: str | None = "") -> Any:
        """Call the query API. Authenticated by default (DF-9).

        `key=""` means the dev read key; `key=None` means send NO credential,
        which is how F1 checks the API refuses anonymous callers.
        """
        url = f"http://localhost:8080{path}"
        request = urllib.request.Request(url)
        token = read_key if key == "" else key
        if token:
            request.add_header("x-api-key", token)
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read())

    try:
        api("/health")
    except (urllib.error.URLError, TimeoutError) as exc:
        record("C1 query API reachable", False, f"{exc}")
    else:
        overview = api("/api/v1/overview?window_minutes=180")
        record(
            "C1 overview reports servers, tools and a failure breakdown",
            overview["servers"] > 0 and overview["tools"] > 0
            and sum(overview["failure_breakdown"].values()) > 0,
            f"servers={overview['servers']} tools={overview['tools']} "
            f"calls={overview['calls']} classified={overview['classified_ratio']}",
        )

        tool_rows = api("/api/v1/tools?window_minutes=180")
        with_kinds = {
            kind
            for row in tool_rows
            for kind, count in row["failure_breakdown"].items()
            if count and kind != "ok"
        }
        record(
            "C2 per-tool failure breakdown distinguishes failure kinds",
            len(with_kinds) >= 4,
            f"{sorted(with_kinds)}",
        )

        errors_page = api("/api/v1/errors?window_minutes=180&limit=20")
        categories = {item["failure_category"] for item in errors_page["items"]}
        record(
            "C3 /errors excludes ok and pending_input",
            bool(errors_page["items"])
            and not (categories & {"ok", "pending_input", ""}),
            f"{sorted(categories)} over {len(errors_page['items'])} traces",
        )

        listing = api("/api/v1/traces?window_minutes=180&tool=fetch_status&limit=1")
        detail = api(f"/api/v1/traces/{listing['items'][0]['trace_id']}")
        has_child = any(span["downstream_kind"] for span in detail["spans"])
        rooted = any(
            span["span_id"] == detail["root_span_id"] and span["depth"] == 0
            for span in detail["spans"]
        )
        record(
            "C4 trace detail resolves a root and nests downstream spans",
            has_child and rooted and detail["span_count"] > 1,
            f"{detail['span_count']} spans, root={detail['root_span_id'][:12]}, "
            f"kinds={[s['downstream_kind'] for s in detail['spans'] if s['downstream_kind']]}",
        )

        # Cross-tenant isolation, now proved the only way that means anything:
        # with a SECOND ORG'S KEY.
        #
        # This used to send `?tenant=someone-else` and check the answer was
        # empty. That assertion died with DF-9 -- and it is worth being precise
        # about why, because it looked like it was testing isolation. It was
        # testing that an unknown STRING matched no rows. Now that tenancy comes
        # from the key, the parameter is inert, so the old form passed a real
        # tenant's data back and "failed" while the boundary it named was
        # working perfectly. An assertion that cannot fail for the right reason
        # cannot pass for one either.
        from control.repository import ControlPlane as ControlPlaneClient

        try:
            cp = ControlPlaneClient(os.getenv(
                "CONTROL_PLANE_DSN",
                "postgresql://mcpobs:mcpobs@localhost:5433/mcpobs_control",
            ))
            other = cp.create_org("verify-isolation")
            other_project = cp.create_project(other.id, "default", environment="local")
            other_key = cp.issue_key(
                other.id, other_project.id, scopes=("read",), name="verify isolation"
            )
            isolated = api("/api/v1/overview?window_minutes=10080", key=other_key.token)
            record(
                "C5 a different org's key sees none of this org's data",
                isolated["calls"] == 0 and isolated["servers"] == 0,
                f"calls={isolated['calls']} servers={isolated['servers']} "
                f"for org {other.slug}, against {overview['calls']} for local",
            )
        except Exception as exc:
            record("C5 a different org's key sees none of this org's data", False, f"{exc}")

    # ---------------- D5: payload capture ----------------------------------
    print("\n--- D5: request/response capture ---")
    captured = ch.query(
        "SELECT count(), countIf(output_preview IS NOT NULL) FROM spans_raw "
        "WHERE input_preview IS NOT NULL AND timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0]
    record(
        "D5 request and response are captured when enabled",
        captured[0] > 0 and captured[1] > 0,
        f"{captured[0]} spans with a request, {captured[1]} with a response",
    )

    # Redaction is pattern-based and therefore incomplete, but the obvious
    # shapes must not survive. A secret that reaches storage cannot be recalled.
    leaked = ch.query(
        "SELECT count() FROM spans_raw WHERE "
        "  input_preview ILIKE '%\"api_key\": \"sk-%' "
        "  OR output_preview ILIKE '%Bearer ey%'"
    ).result_rows[0][0]
    # Every attribute, not just the payload previews. The spec requires
    # `Authorization: Bearer ...` on EVERY MCP-over-HTTP request, so a customer
    # capturing request headers captures a token on every span -- and the
    # redactor used to scrub only six predicted keys.
    creds = ch.query(
        "SELECT countIf(position(toString(span_attributes), 'Bearer ey') > 0) "
        "     + countIf(position(toString(span_attributes), 'Bearer sk-') > 0) "
        "     + countIf(position(toString(resource_attributes), 'Bearer ') > 0) "
        "FROM spans_raw"
    ).result_rows[0][0]
    record(
        "D5c no credential shape survives in ANY attribute",
        creds == 0,
        f"{creds} span(s) carry a bearer token or key outside the payload columns",
    )

    record(
        "D5b obvious secret shapes are redacted before storage",
        leaked == 0,
        f"{leaked} previews contain an unredacted key or bearer token",
    )

    # ---------------- D6: downstream + client identity ---------------------
    print()
    print("--- D6: downstream capture and client identity ---")

    # D60 was revised by measurement (D67): request bodies and BOTH sets of
    # headers are capturable through the instrumentation's hooks.
    http = ch.query(
        "SELECT countIf(http_request_body != ''), countIf(http_request_headers != ''), "
        "       countIf(http_response_headers != '') "
        "FROM spans_raw WHERE downstream_kind = 'http' "
        "  AND timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0]
    record(
        "D6 downstream HTTP request bodies and headers are captured",
        http[0] > 0 and http[1] > 0 and http[2] > 0,
        f"{http[0]} bodies, {http[1]} request-header sets, {http[2]} response-header sets",
    )

    # The allow-list, checked against the database rather than only in a unit
    # test. `submit_order` deliberately sends a bearer token: if the allow-list
    # ever becomes a deny-list, this is what catches it.
    credential_leak = ch.query(
        "SELECT count() FROM spans_raw WHERE "
        "  http_request_headers ILIKE '%authorization%' "
        "  OR http_request_headers ILIKE '%demo-token%' "
        "  OR http_request_headers ILIKE '%cookie%'"
    ).result_rows[0][0]
    record(
        "D6b credential headers never reach storage",
        credential_leak == 0,
        f"{credential_leak} rows carry an authorization or cookie header",
    )

    # There must be NO response-body column (D69). An always-empty column reads
    # as "this call had no response body", which is a wrong answer rather than
    # a missing one -- so its ABSENCE is the assertion.
    response_body_column = ch.query(
        "SELECT count() FROM system.columns WHERE database = 'mcpobs' "
        "  AND table = 'spans_raw' AND name = 'http_response_body'"
    ).result_rows[0][0]
    record(
        "D6c no http_response_body column exists",
        response_body_column == 0,
        "the client span ends before httpx reads a response body"
        if response_body_column == 0
        else "an always-empty column survived migration 011",
    )

    # D71: client identity without payload capture. Asserted on SUCCESSFUL
    # spans specifically -- recording it only on failures would answer "which
    # clients call which tools" with a breakdown of failures.
    clients = ch.query(
        "SELECT countIf(client_name != ''), uniqExact(client_name), "
        "       countIf(client_name != '' AND mcp_is_error = 0) "
        "FROM spans_raw WHERE mcp_method != '' "
        "  AND timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0]
    record(
        "D6d client identity is recorded on MCP spans, successes included",
        clients[0] > 0 and clients[2] > 0,
        f"{clients[0]} spans name a client ({clients[1]} distinct), {clients[2]} of them successful",
    )

    # D72: DF-12 closed. Derived from the redacted statement, so a literal
    # cannot ride out inside a label that a metrics backend would export.
    # Scoped to the CURRENT normalization version, which is what this asserts
    # about: that the normalizer running now derives these fields. Spans written
    # by an earlier version are correctly showing what that version produced,
    # and only a replay would change them -- counting them here would make the
    # assertion fail for history rather than for behaviour. argMax does not help
    # either: those are different spans, not older rows for the same ones.
    db = ch.query(
        "SELECT countIf(db_operation != ''), countIf(db_collection != ''), "
        "       countIf(db_statement != '') "
        "FROM spans_raw WHERE downstream_kind = 'db' "
        "  AND timestamp > now() - INTERVAL 30 MINUTE "
        "  AND normalization_version = (SELECT max(normalization_version) FROM spans_raw)"
    ).result_rows[0]
    record(
        "D6e db.operation and db.collection are derived from the statement",
        db[2] > 0 and db[0] == db[2] and db[1] == db[2],
        f"{db[0]}/{db[2]} have an operation, {db[1]}/{db[2]} a collection",
    )

    # ---------------- E: rollups and the clock -----------------------------
    print()
    print("--- E: rollups (DF-7) and clock resolution (DF-4) ---")

    # THE assertion for DF-7, and it has to be run in two steps to mean
    # anything. A materialized view cannot honour the argMax over
    # normalization_version that every other read obeys (D24), so a replay makes
    # the rollup over-count -- silently, with nothing erroring.
    #
    # This script has ALREADY replayed a span by the time it gets here: B11b
    # deliberately inserts the same span again under a different token, to prove
    # a bug-fix replay is not discarded. So the drift below is not hypothetical
    # and not a test artefact -- it is the real failure mode, reproduced.
    #
    # E1a measures it. E1b proves `make rollups` repairs it. Asserting only the
    # second would hide that the first is possible; asserting only the first
    # would leave the repair untested.
    def rollup_vs_raw():
        rollup = ch.query("SELECT sum(calls), sum(errors) FROM tool_metrics_1m").result_rows[0]
        raw = ch.query(
            "SELECT count(), sum(err) FROM ("
            "  SELECT argMax(mcp_is_error, normalization_version) AS err "
            "  FROM spans_raw GROUP BY tenant_id, project_id, span_id)"
        ).result_rows[0]
        return rollup, raw

    before_rollup, before_raw = rollup_vs_raw()
    record(
        "E1a a replay is visible as rollup drift, not hidden",
        before_rollup[0] >= before_raw[0],
        f"rollup {before_rollup[0]} vs raw {before_raw[0]} calls"
        + (" (drift, as expected after B11b's replay)"
           if before_rollup[0] != before_raw[0] else " (no replay in this window)"),
    )

    from scripts.recompute_rollups import RollupRecomputer

    recomputer = RollupRecomputer()
    for day in recomputer.dates(None):
        recomputer.recompute(day)
    after_rollup, after_raw = rollup_vs_raw()
    record(
        "E1b `make rollups` reconciles the rollup with the raw table",
        after_rollup[0] == after_raw[0] and after_rollup[1] == after_raw[1],
        f"rollup {after_rollup[0]} calls / {after_rollup[1]} errors "
        f"vs raw {after_raw[0]} / {after_raw[1]}",
    )

    # The rollup is only worth reading if the API actually reads it. Comparing
    # the endpoint against the raw table closes the loop: a rollup nobody reads
    # is what `trace_summaries` was for four days.
    api_overview = api("/api/v1/overview?window_minutes=10080")
    # SCOPED TO THE KEY'S TENANT. The API answers for one tenant; counting raw
    # rows across all of them compares two different populations -- the same
    # class of mistake B4 made. It only became visible once a second tenant
    # existed, which is its own argument for having one locally.
    raw_calls = ch.query(
        "SELECT count() FROM ("
        "  SELECT argMax(mcp_method, normalization_version) AS m "
        "  FROM spans_raw WHERE tenant_id = 'local' "
        "  GROUP BY tenant_id, project_id, span_id) "
        "WHERE m = 'tools/call'"
    ).result_rows[0][0]
    record(
        "E2 /overview serves rollup numbers that match raw",
        api_overview["calls"] == raw_calls,
        f"api {api_overview['calls']} vs raw {raw_calls} tool calls",
    )

    # DF-3: the aggregate must not outlive the data it summarises.
    parts = ch.query(
        "SELECT count(DISTINCT partition) FROM system.parts "
        "WHERE database = 'mcpobs' AND table = 'trace_summaries' AND active"
    ).result_rows[0]
    # `create_table_query` carries the TTL clause; this ClickHouse exposes no
    # dedicated column for it, and asserting on the DDL is anyway closer to what
    # is being claimed -- that the table was DECLARED droppable by partition.
    ttl = ch.query(
        "SELECT count() FROM system.tables WHERE database = 'mcpobs' "
        "  AND name IN ('trace_summaries', 'tool_metrics_1m') "
        "  AND partition_key != '' AND create_table_query LIKE '%TTL %'"
    ).result_rows[0][0]
    record(
        "E3 aggregate tables are partitioned and have a TTL",
        ttl == 2,
        f"{ttl}/2 of trace_summaries and tool_metrics_1m are droppable by partition"
        f" ({parts[0]} live partition(s))",
    )

    # DF-3's stated cost: a trace straddling midnight becomes two rows. Distinct
    # traces is what B4 always meant, so the split must not change the answer.
    #
    # Run AFTER the recompute above, and that ordering is the finding rather
    # than a convenience. `trace_summaries` is a materialized view too, so it
    # double-counts a replayed span exactly as the new rollup does -- it had
    # been doing so since Day 2, invisibly, because B4 counts distinct TRACES
    # and a replayed span adds no trace. Writing this assertion against SPANS is
    # what surfaced it.
    summary = ch.query(
        "SELECT uniqExact(trace_id), sum(span_count) FROM trace_summaries"
    ).result_rows[0]
    raw_traces = ch.query(
        "SELECT uniqExact(trace_id), count() FROM ("
        "  SELECT trace_id FROM spans_raw "
        "  GROUP BY tenant_id, project_id, trace_id, span_id)"
    ).result_rows[0]
    record(
        "E4 trace_summaries reconciles with raw, by trace AND by span",
        summary[0] == raw_traces[0] and summary[1] == raw_traces[1],
        f"{summary[0]} traces / {summary[1]} spans vs raw {raw_traces[0]} / {raw_traces[1]}",
    )

    # DF-4 becomes a product behaviour rather than a verify-only warning. The
    # register said it plainly: Linux is nanosecond-grade so OUR production is
    # probably fine, A CUSTOMER ON WINDOWS IS NOT -- which makes it a caveat
    # that has to reach the console, not just this script.
    tick = ch.query(
        "SELECT min(min_tick_ns) FROM tool_metrics_1m"
    ).result_rows[0][0]
    latency = api_overview["latency"]
    coarse = bool(tick and tick > 100_000)
    record(
        "E5 the observed clock tick is measured and exposed",
        latency["clock_tick_ms"] > 0,
        f"clock ticks every {latency['clock_tick_ms']:.4f}ms (observed, not assumed)",
    )
    record(
        "E5b a clock too coarse for the percentiles says so in the API",
        (not coarse) or bool(latency["clock_warning"]),
        latency["clock_warning"] or "clock is fine; no caveat needed",
    )

    # ---------------- F: auth, tenancy and the archive (DF-9) --------------
    print()
    print("--- F: auth, tenancy and the archive ---")

    import urllib.error as _urlerr

    # F1. The read plane refuses anonymous callers. Until DF-9 landed, `?tenant=`
    # was a query parameter and every endpoint was world-readable.
    try:
        api("/api/v1/overview", key=None)
        anonymous_allowed = True
    except _urlerr.HTTPError as exc:
        anonymous_allowed = exc.code != 401
    record(
        "F1 the query API refuses an unauthenticated caller",
        not anonymous_allowed,
        "401 without a key" if not anonymous_allowed else "ANONYMOUS READ IS OPEN",
    )

    # F2. Scopes are real. An ingest key identifies the same org but must not
    # read: the two live in different places -- a server process and a browser --
    # so one being compromised must not imply the other.
    try:
        api("/api/v1/overview", key=_dev_key("MCPOBS_INGEST_KEY"))
        ingest_can_read = True
    except _urlerr.HTTPError as exc:
        ingest_can_read = exc.code != 401
    record(
        "F2 an ingest key cannot read",
        not ingest_can_read,
        "ingest scope refused at the read plane" if not ingest_can_read
        else "AN INGEST KEY CAN READ THE CONSOLE",
    )

    # F3. THE write-side boundary (Architecture.md §5.1): a customer must not be
    # able to write into another tenant by setting a resource attribute. Fired
    # as a real OTLP payload against the real gateway, not a unit test -- the
    # unit test in tests/test_control_plane.py checks the function; this checks
    # that the function is actually on the path.
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

    forged = ExportTraceServiceRequest()
    rs = ResourceSpans()
    for key, value in (("tenant.id", "somebody-else"), ("service.name", "impostor")):
        rs.resource.attributes.append(
            KeyValue(key=key, value=AnyValue(string_value=value))
        )
    scope_spans = ScopeSpans()
    # A FRESH trace id per run. The first version used a constant one, so every
    # verify run added another span to the same synthetic trace -- which then
    # became the largest trace in the database and got picked up by A5, whose
    # parent links naturally do not resolve because the spans are unrelated. A
    # probe must not accumulate into a fixture other assertions read.
    import secrets as _secrets

    span = Span(
        name="forged", trace_id=_secrets.token_bytes(16), span_id=_secrets.token_bytes(8),
        start_time_unix_nano=int(time.time() * 1e9),
        end_time_unix_nano=int(time.time() * 1e9) + 1_000_000,
    )
    scope_spans.spans.append(span)
    rs.scope_spans.append(scope_spans)
    forged.resource_spans.append(rs)

    gateway = urllib.request.Request(
        "http://localhost:4319/v1/traces",
        data=forged.SerializeToString(),
        headers={
            "content-type": "application/x-protobuf",
            "x-api-key": _dev_key("MCPOBS_INGEST_KEY"),
        },
    )
    try:
        with urllib.request.urlopen(gateway, timeout=20) as response:
            accepted = response.status < 300
    except Exception as exc:
        accepted = False
        print(f"      gateway rejected the probe: {exc}")

    if accepted:
        deadline = time.time() + 45
        landed = 0
        while time.time() < deadline:
            landed = ch.query(
                "SELECT count() FROM spans_raw WHERE span_name = 'forged'"
            ).result_rows[0][0]
            if landed:
                break
            time.sleep(2)
        stolen = ch.query(
            "SELECT count() FROM spans_raw "
            "WHERE span_name = 'forged' AND tenant_id = 'somebody-else'"
        ).result_rows[0][0]
        record(
            "F3 a forged tenant.id is overwritten by the authenticated one",
            landed > 0 and stolen == 0,
            f"{landed} forged span(s) stored, {stolen} under the claimed tenant"
            + ("" if landed else "  -- span never arrived, so this proved nothing"),
        )
    else:
        record("F3 a forged tenant.id is overwritten", False, "gateway did not accept")

    # F4. The read side of the same boundary. `?tenant=` used to select tenancy;
    # it must now be inert rather than deprecated -- an optional boundary is not
    # one.
    honest = api("/api/v1/overview?window_minutes=1440")
    attempted = api("/api/v1/overview?window_minutes=1440&tenant=somebody-else&project=x")
    record(
        "F4 a tenant query parameter cannot change what a key sees",
        honest["calls"] == attempted["calls"],
        f"{honest['calls']} calls with and without ?tenant= override",
    )

    # F5. The archive exists, is per-tenant, and can be READ BACK. An archive
    # nobody has restored is a backup nobody has restored.
    try:
        import boto3

        from archiver.archiver import unframe

        s3 = boto3.client(
            "s3", endpoint_url=os.getenv("S3_ENDPOINT", "http://localhost:9002"),
            aws_access_key_id="mcpobs", aws_secret_access_key="mcpobs-secret",
            region_name="us-east-1",
        )
        contents = s3.list_objects_v2(Bucket="mcpobs-archive").get("Contents", [])
        prefixes = {o["Key"].split("/", 1)[0] for o in contents}
        # `unknown/` is EXPECTED here and is not a failure: A9 deliberately
        # produces an undecodable message, and the archiver files those verbatim
        # rather than dropping them -- losing bytes is worse than filing them
        # awkwardly. What must hold is that decodable traffic is filed under its
        # real tenant, so the assertion is on the presence of `local`, not the
        # absence of `unknown`.
        record(
            "F5 the archive files decodable traffic under its tenant prefix",
            bool(contents) and "local" in prefixes,
            f"{len(contents)} object(s) under {sorted(prefixes)}"
            + ("  (`unknown` is A9's deliberate poison message)"
               if "unknown" in prefixes else ""),
        )
        real = [o for o in contents if not o["Key"].startswith("unknown/")]
        if real:
            # Deliberately not "whichever is newest": the newest may be A9's
            # poison message, and asserting that garbage fails to parse would
            # prove nothing about the archive format.
            newest = max(real, key=lambda o: o["LastModified"])
            blob = s3.get_object(Bucket="mcpobs-archive", Key=newest["Key"])["Body"].read()
            messages = unframe(blob)
            parsed = ExportTraceServiceRequest()
            parsed.ParseFromString(messages[0])
            record(
                "F5b an archived object parses back into OTLP",
                bool(messages) and len(parsed.resource_spans) > 0,
                f"{len(messages)} message(s), "
                f"{len(parsed.resource_spans)} ResourceSpans in the first",
            )
    except Exception as exc:
        record("F5 the archive holds objects", False, f"{exc}")

    # F6. Invite-only, checked as a property rather than a policy. A user row
    # can only exist because an invite was redeemed.
    try:
        from control import ControlPlane

        cp = ControlPlane(
            os.getenv("CONTROL_PLANE_DSN",
                      "postgresql://mcpobs:mcpobs@localhost:5433/mcpobs_control")
        )
        orphans = cp._one(
            "SELECT count(*) AS n FROM users u "
            "WHERE NOT EXISTS (SELECT 1 FROM invites i "
            "                  WHERE i.accepted_by = u.id AND i.accepted_at IS NOT NULL)"
        )
        record(
            "F6 every user exists because an invite was redeemed",
            orphans is not None and orphans["n"] == 0,
            f"{orphans['n'] if orphans else '?'} user(s) with no redeemed invite",
        )

        # A redeemed invite is single use. Redeeming twice would let one leaked
        # code seed an organisation indefinitely.
        reused = cp._one(
            "SELECT count(*) AS n FROM invites "
            "WHERE accepted_at IS NOT NULL AND accepted_by IS NULL"
        )
        record(
            "F6b no invite was consumed without producing a user",
            reused is not None and reused["n"] == 0,
            f"{reused['n'] if reused else '?'} invite(s) accepted with no user",
        )
    except Exception as exc:
        record("F6 every user exists because an invite was redeemed", False, f"{exc}")

    # ---------------- G: transport authorization (DF-22) -------------------
    print()
    print("--- G: transport authorization ---")

    # Spawns its OWN auth-enabled server and provokes a 401. Asserting over
    # whatever happens to be in the table would go vacuous the moment nobody
    # ran an auth demo -- the same way B9 sat green for days with no
    # subscription span in existence.
    auth_port = 8021
    auth_proc = subprocess.Popen(
        [sys.executable, "-m", "demo_server.server", "--http", "--port", str(auth_port)],
        cwd=REPO_ROOT,
        env={**os.environ, "DEMO_AUTH": "1", "DEMO_HTTP_PORT": str(auth_port)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        from demo_server.scenarios import _wait_for_port, run_auth_scenario

        if _wait_for_port(auth_port, timeout=30):
            for line in asyncio.run(run_auth_scenario(auth_port)):
                print(f"     {line.strip()}")
            time.sleep(12)  # let the batch reach ClickHouse

        current = ch.query("SELECT max(normalization_version) FROM spans_raw").result_rows[0][0]
        rows = ch.query(
            "SELECT count(), countIf(failure_category = 'unauthorized'), "
            "       countIf(mcp_is_error = 1) "
            "FROM spans_raw WHERE span_kind = 'SERVER' AND http_status_code = 401 "
            f"  AND normalization_version = {int(current)}"
        ).result_rows[0]
        record(
            "G1 a 401 that never reached an MCP method is visible and classified",
            rows[0] > 0 and rows[1] == rows[0],
            f"{rows[0]} transport 401(s), {rows[1]} classified as unauthorized"
            + ("  -- NO 401 AT ALL, so this proved nothing" if not rows[0] else ""),
        )
        record(
            "G1b a 401 is NOT counted as a server failure",
            rows[2] == 0,
            "the spec's own flow opens with an unauthenticated request answered by a 401"
            if rows[2] == 0 else f"{rows[2]} 401(s) wrongly counted as errors",
        )

        # The HTTP layer must be present at all -- this is what DF-22 lacked.
        transport = ch.query(
            "SELECT count() FROM spans_raw WHERE span_kind = 'SERVER' "
            "  AND span_name LIKE 'POST /%' AND timestamp > now() - INTERVAL 30 MINUTE"
        ).result_rows[0][0]
        record(
            "G2 the HTTP layer beneath MCP is observed",
            transport > 0,
            f"{transport} transport span(s); before DF-22 there were none at all",
        )
    finally:
        auth_proc.terminate()

    # ---------------- B7: freshness, the headline metric -------------------
    # Architecture.md §9.1 names end-to-end freshness -- span event time to
    # queryable time -- as the one number that says whether the pipeline is
    # healthy. It had never been measured. Asserted here so it is checked on
    # every run rather than being a metric nobody looks at.
    print("\n--- B7: freshness ---")
    p50, p95, newest = ch.query(
        "SELECT quantile(0.50)(dateDiff('millisecond', timestamp, ingested_at)), "
        "       quantile(0.95)(dateDiff('millisecond', timestamp, ingested_at)), "
        "       max(ingested_at) "
        "FROM spans_raw WHERE timestamp > now() - INTERVAL 30 MINUTE"
    ).result_rows[0]
    record(
        "B7 end-to-end freshness p95 under 60s",
        (p95 or 0) < 60_000,
        f"p50={(p50 or 0) / 1000:.1f}s p95={(p95 or 0) / 1000:.1f}s newest={newest}",
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
    # The partition-stall check. A dead-lettered message whose offset is never
    # committed leaves lag stuck forever and re-DLQs on every restart. Without
    # this assertion the pipeline looks healthy while quietly replaying poison.
    lag = wait_for_zero_lag(timeout=60)
    record(
        "A9c consumer lag drains to zero after the DLQ",
        lag == 0,
        f"total lag = {lag} (a stuck offset means the partition replays the poison)",
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
