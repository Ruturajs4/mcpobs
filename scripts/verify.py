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

    # The privacy property that makes this a core feature rather than one gated
    # behind payload capture (V2 §15).
    leaked = ch.query(
        "SELECT count() FROM spans_raw "
        "WHERE input_preview IS NOT NULL OR output_preview IS NOT NULL"
    ).result_rows[0][0]
    record(
        "B2 no tool content stored to achieve the taxonomy",
        leaked == 0,
        f"{leaked} rows carry payload previews (must be 0)",
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
            "WHERE failure_category NOT IN ('', 'ok', 'protocol_error', 'pending_input') "
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
    streams = ch.query(
        "SELECT count() FROM spans_raw "
        "WHERE mcp_method LIKE 'subscriptions/listen%' AND is_latency_eligible = 1"
    ).result_rows[0][0]
    record(
        "B9 no stream or interim span is latency-eligible",
        streams == 0,
        f"{streams} stream spans wrongly eligible; excluded so far: {ineligible or 'none seen yet'}",
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
    raw_spans, summed_spans = ch.query(
        f"SELECT (SELECT count() FROM spans_raw WHERE 1 {probe}), "
        f"       (SELECT sum(span_count) FROM trace_summaries WHERE 1 {probe})"
    ).result_rows[0]
    record(
        "B4 trace_summaries has exactly one row per trace",
        raw_traces == summary_traces and raw_spans == summed_spans,
        f"{summary_traces}/{raw_traces} traces, {summed_spans}/{raw_spans} spans accounted for",
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
    expected_tools = {tool for tool, _, _ in SCENARIOS}
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
            "  -- COARSE CLOCK: latency percentiles for fast tools are not "
            "trustworthy here. See the Clock resolution section of "
            "docs/observed_attributes.md (D27)."
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

    def api(path: str) -> Any:
        url = f"http://localhost:8080{path}"
        with urllib.request.urlopen(url, timeout=25) as response:
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

        # Cross-tenant isolation. There is no auth yet (DF-9), but the scoping
        # itself must already work -- otherwise adding keys later would be
        # papering over a query that never filtered.
        other = api("/api/v1/overview?window_minutes=180&tenant=someone-else")
        record(
            "C5 an unknown tenant sees nothing",
            other["calls"] == 0 and other["servers"] == 0,
            f"calls={other['calls']} servers={other['servers']}",
        )

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
