"""ClickHouse reads.

THREE RULES, ENFORCED HERE SO NO ENDPOINT CAN FORGET THEM
    1. Tenant scoping is applied by this layer, never by a caller. An endpoint
       that forgets a `WHERE tenant_id` leaks another customer's telemetry, and
       that must not be possible to write by accident (V2 Â§13.1).
    2. Every read of `spans_raw` goes through `LATEST_SPANS`, which resolves
       `argMax(..., normalization_version)` per `(trace_id, span_id)`. A replay
       leaves several versions of the same span in the table; a naive read mixes
       corrected rows with the buggy ones it was meant to replace (D24).
    3. Latency aggregates filter `is_latency_eligible`. A `subscriptions/listen`
       span's duration is a stream lifetime and would destroy a p95 (D29).

Parameters are always bound, never interpolated.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from query.dtos import (
    NOT_A_FAILURE,
    CapabilityPage,
    CapabilityRow,
    FailureBreakdown,
    FilterOptions,
    LatencyStats,
    Overview,
    Page,
    ServerSummary,
    SpanDetail,
    SpanDTO,
    ToolSummary,
    TraceDetail,
    TraceSummary,
)
from query.filters import HAVING, WHERE, Filters

# Rule 2, in one place. Every span read starts here.
#
# `argMax(col, normalization_version)` picks the newest normalization of each
# span. Grouping by (trace_id, span_id) is what makes a replay safe: the
# corrected v5 row wins over the buggy v2 row rather than appearing beside it.
LATEST_SPANS = """
SELECT
    trace_id,
    span_id,
    argMax(parent_span_id, normalization_version)      AS parent_span_id,
    argMax(timestamp, normalization_version)           AS span_time,
    argMax(duration_ns, normalization_version)         AS duration_ns,
    argMax(span_name, normalization_version)           AS span_name,
    argMax(span_kind, normalization_version)           AS span_kind,
    argMax(status_code, normalization_version)         AS status_code,
    argMax(service_name, normalization_version)        AS service_name,
    argMax(service_version, normalization_version)     AS service_version,
    argMax(deployment_environment, normalization_version) AS environment,
    argMax(mcp_method, normalization_version)          AS mcp_method,
    argMax(mcp_tool_name, normalization_version)       AS mcp_tool_name,
    argMax(mcp_prompt_name, normalization_version)     AS mcp_prompt_name,
    argMax(mcp_resource_uri, normalization_version)    AS mcp_resource_uri,
    argMax(transport, normalization_version)           AS transport,
    argMax(mcp_is_error, normalization_version)        AS mcp_is_error,
    argMax(failure_detail, normalization_version)      AS failure_detail,
    argMax(failure_category, normalization_version)    AS failure_category,
    argMax(failure_kind_source, normalization_version) AS failure_kind_source,
    argMax(is_latency_eligible, normalization_version) AS is_latency_eligible,
    argMax(downstream_kind, normalization_version)     AS downstream_kind,
    argMax(http_method, normalization_version)         AS http_method,
    argMax(http_status_code, normalization_version)    AS http_status_code,
    argMax(db_system, normalization_version)           AS db_system,
    argMax(gen_ai_model, normalization_version)        AS gen_ai_model,
    argMax(ingested_at, normalization_version)         AS ingested_at
FROM spans_raw
-- NOTE the alias `span_time`, not `timestamp`. Aliasing an aggregate with the
-- same name as its source column makes ClickHouse resolve `timestamp` in this
-- WHERE to the aggregate, and it rejects the query with ILLEGAL_AGGREGATION.
WHERE tenant_id = {tenant:String}
  AND project_id = {project:String}
  AND timestamp >= {since:DateTime}
GROUP BY trace_id, span_id
"""

def _number(value: Any) -> float:
    """None/NaN/Inf -> 0.0.

    `value or 0` is NOT safe here: NaN is truthy in Python, so it passes
    straight through and then json.dumps raises "Out of range float values are
    not JSON compliant" -- a 500. ClickHouse returns NaN from `quantile()` over
    an empty set, which is exactly what a brand-new tenant with no telemetry
    looks like. Their first-ever page load would have been an error.
    """
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


#: The four capability surfaces, each a different `mcp_method` asked the same
#: question. `name_col` is what identifies an individual item within the kind.
#: `protocol` deliberately catches EVERYTHING else -- tools/list,
#: server/discover, subscriptions/listen, tasks/* -- because 38% of stored
#: protocol activity had no home in the console and a closed list would let the
#: next new method vanish the same way (D8: never enumerate MCP methods).
CAPABILITY_KINDS: dict[str, tuple[str, str]] = {
    "tool": ("tools/call", "mcp_tool_name"),
    "prompt": ("prompts/get", "mcp_prompt_name"),
    "resource": ("resources/read", "mcp_resource_uri"),
    "protocol": ("", "mcp_method"),
}

FRESHNESS_WINDOW_MINUTES = 15
"""Freshness is a "right now" health signal, never a historical aggregate."""

ROLLUP = """
    SELECT
        bucket, service_name, mcp_method, mcp_tool_name, failure_category,
        sum(calls)             AS calls,
        sum(errors)            AS errors,
        quantilesMerge(0.50, 0.95, 0.99)(latency) AS latency,
        sum(latency_count)     AS latency_count,
        max(latency_max)       AS latency_max,
        sum(zero_duration)     AS zero_duration,
        min(min_tick_ns)       AS min_tick_ns,
        sum(helper_classified) AS helper_classified,
        max(last_seen)         AS last_seen,
        anyLast(service_version) AS service_version,
        anyLast(environment)     AS environment
    FROM tool_metrics_1m
    WHERE tenant_id = {tenant:String} AND project_id = {project:String}
      AND bucket >= toStartOfMinute({since:DateTime})
    GROUP BY bucket, service_name, mcp_method, mcp_tool_name, failure_category
"""
"""The minute rollup, re-aggregated (DF-7).

An AggregatingMergeTree returns one row per unmerged part, so a reader MUST
group -- reading it without a GROUP BY silently returns partial states. Grouping
here also collapses the categories back together for callers that do not want
the breakdown.

Deliberately NOT wrapped in the `LATEST_SPANS` argMax: the rollup is built by a
materialized view, which cannot see version history at all. `scripts/
recompute_rollups.py` is what reconciles it after a replay, and assertion E1
checks the two agree on every run.
"""

#: The error list's definition of a failure, from the SAME constant the overview
#: and the ?status= filter use. It previously excluded only `pending_input`, so
#: the Errors page listed cancellations and 401s that the headline error rate
#: said were not errors -- 62 such traces over 24h of real data.
_NOT_A_FAILURE_SQL = ", ".join(f"'{c}'" for c in NOT_A_FAILURE)

#: Most spans one trace returns. `GET /traces/{id}` had no limit at all: it
#: returned every span PLUS a full ~60-column SpanDetail for each, measured at
#: 2.4 KB per span. A 10,000-span trace would have been a 24 MB response, and a
#: browser asked to draw 10,000 waterfall rows.
#:
#: That is not a hypothetical shape for this product. Progress reports are
#: already capped at 200 because "a tool can generate spans faster than it does
#: work" -- but that reasoning was only ever applied to progress. The unbounded
#: case is ordinary: a tool that loops. `for row in rows: cur.execute(...)` over
#: ten thousand rows is ten thousand child spans in one trace.
#:
#: 2,000 is well past any trace a human reads span-by-span and well short of the
#: sizes that hurt.
TRACE_SPAN_CAP = 2_000

#: Above this many spans, the per-span detail map is omitted from the response.
#: `detail` exists so that clicking a span in the waterfall needs no second
#: request, which is worth it for a twenty-span trace and is most of the payload
#: for a two-thousand-span one.
TRACE_DETAIL_CAP = 300

#: Most rows a capability table returns. Aggregation is by NAME, so this bounds
#: distinct tools/prompts/resources rather than call volume -- a few dozen for a
#: real server. The cap exists because the number is unbounded in principle, and
#: because each row previously cost two extra queries; that is fixed, but a
#: table nobody can read is still not worth building. Sorted first, so what is
#: dropped is the tail of whatever the reader asked to sort by.
CAPABILITY_ROW_CAP = 200

#: A percentile is meaningless once the clock's tick approaches it. Below this
#: multiple of the observed tick, the console says so instead of printing a
#: confident number (DF-4).
CLOCK_TRUST_MULTIPLE = 10

#: ...and a p50 well above the tick still lies if most samples floored to zero.
CLOCK_ZERO_FRACTION = 0.20

CATEGORIES = (
    "ok",
    "tool_error",
    "server_exception",
    "unknown_tool",
    "invalid_arguments",
    "protocol_error",
    "pending_input",
    "cancelled",
    "unauthorized",
    "forbidden",
    "unclassified",
)


class SpanRepository:
    def __init__(self, client: Client | None = None, **connect: Any) -> None:
        self._client = client
        self._connect = connect

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = clickhouse_connect.get_client(**self._connect)
        return self._client

    # -- helpers -----------------------------------------------------------
    def _scope(self, tenant: str, project: str, since: datetime) -> dict[str, Any]:
        return {"tenant": tenant, "project": project, "since": since}

    def _rows(self, sql: str, params: dict[str, Any]) -> Sequence[Sequence[Any]]:
        return self.client.query(sql, parameters=params).result_rows

    @staticmethod
    def _breakdown(pairs: Sequence[Sequence[Any]]) -> FailureBreakdown:
        counts = {c: 0 for c in CATEGORIES}
        for category, count in pairs:
            if category in counts:
                counts[category] += count
        return FailureBreakdown(**counts)

    @staticmethod
    def _clock_warning(p50_ms: float, tick_ms: float, count: int, zeros: int) -> str:
        """Whether the host clock can support the percentiles above it (DF-4).

        DF-4 sat on WATCH being reported by `make verify` every run -- which
        means it was told to us and never to the customer. The number in the
        console was the one nobody had qualified, and the entry itself says the
        quiet part: Linux is nanosecond-grade so OUR production is likely fine,
        A CUSTOMER ON WINDOWS IS NOT. That makes it a product caveat, not a
        test-rig footnote.

        Both tests matter and neither subsumes the other. A p50 within a few
        ticks of the clock's resolution is quantisation noise however few zeros
        there are; and a p50 comfortably above the tick still misleads when most
        samples floored to zero, because the surviving non-zero samples are a
        biased tail rather than a sample of the whole.
        """
        if not tick_ms or not count:
            return ""
        if p50_ms and p50_ms < tick_ms * CLOCK_TRUST_MULTIPLE:
            return (
                f"clock ticks every {tick_ms:.3f}ms - p50 is within "
                f"{CLOCK_TRUST_MULTIPLE}x of that, so these percentiles are "
                "quantisation, not latency"
            )
        if zeros and zeros / count > CLOCK_ZERO_FRACTION:
            return (
                f"{round(100 * zeros / count)}% of calls measured 0ms on a "
                f"{tick_ms:.3f}ms clock - percentiles are computed over the "
                "calls slow enough to register"
            )
        return ""

    @classmethod
    def _latency(cls, row: Sequence[Any] | None) -> LatencyStats:
        if not row or not row[0]:
            return LatencyStats()
        count, p50, p95, p99, mx, zeros = row[:6]
        tick = row[6] if len(row) > 6 else None
        p50_ms = round(_number(p50) / 1e6, 3)
        tick_ms = round(_number(tick) / 1e6, 4) if tick else 0.0
        return LatencyStats(
            count=count,
            p50_ms=p50_ms,
            p95_ms=round(_number(p95) / 1e6, 3),
            p99_ms=round(_number(p99) / 1e6, 3),
            max_ms=round(_number(mx) / 1e6, 3),
            zero_duration=int(_number(zeros)),
            clock_tick_ms=tick_ms,
            clock_warning=cls._clock_warning(
                p50_ms, tick_ms, int(count), int(_number(zeros))
            ),
        )

    _LATENCY_SELECT = """
        SELECT count(), quantile(0.50)(duration_ns), quantile(0.95)(duration_ns),
               quantile(0.99)(duration_ns), max(duration_ns), countIf(duration_ns = 0),
               min(if(duration_ns > 0, duration_ns, NULL))
        FROM ({latest}) WHERE is_latency_eligible = 1 {method} {extra}
    """

    def _latency_for(
        self,
        params: dict[str, Any],
        extra: str = "",
        _any_method: bool = False,
        **extra_params: Any,
    ) -> LatencyStats:
        """Latency over eligible spans (D29).

        `_any_method` widens beyond tools/call for prompts, resources and
        protocol methods -- a slow `tools/list` runs on every client connect and
        is a real symptom, so restricting latency to tool calls would hide it.
        """
        method = "" if _any_method else "AND mcp_method = 'tools/call'"
        sql = self._LATENCY_SELECT.format(latest=LATEST_SPANS, method=method, extra=extra)
        rows = self._rows(sql, {**params, **extra_params})
        return self._latency(rows[0] if rows else None)

    # -- reads -------------------------------------------------------------
    def _rollup_latency(self, params: dict[str, Any], extra: str = "", **more: Any) -> LatencyStats:
        """Latency from the rollup's merged quantile states.

        `quantilesMerge` over stored states, not `quantile` over rows -- the
        states already exclude ineligible spans, because the rollup applied
        `is_latency_eligible` at WRITE time (D29). A reader cannot forget it,
        which is the same reasoning that made eligibility a column rather than
        a query-time filter.
        """
        rows = self._rows(
            f"""SELECT sum(latency_count),
                       quantilesMerge(0.50, 0.95, 0.99)(latency)[1],
                       quantilesMerge(0.50, 0.95, 0.99)(latency)[2],
                       quantilesMerge(0.50, 0.95, 0.99)(latency)[3],
                       max(latency_max), sum(zero_duration), min(min_tick_ns)
                FROM tool_metrics_1m
                WHERE tenant_id = {{tenant:String}} AND project_id = {{project:String}}
                  AND bucket >= toStartOfMinute({{since:DateTime}}) {extra}""",
            {**params, **more},
        )
        return self._latency(rows[0] if rows else None)

    def overview(self, tenant: str, project: str, since: datetime, window: int) -> Overview:
        params = self._scope(tenant, project, since)

        # Reads the ROLLUP, not the raw spans (DF-7). This is the widest scan in
        # the product -- every span in the window, for a handful of scalars --
        # and it is the query a dashboard fires on every page load. Assertion E1
        # compares these numbers against the raw table on every verify run, so
        # the second path is continuously checked rather than trusted.
        totals = self._rows(
            f"""SELECT uniqExact(service_name),
                       uniqExactIf(mcp_tool_name, mcp_tool_name != ''),
                       sumIf(calls, mcp_method = 'tools/call'),
                       sumIf(errors, mcp_method = 'tools/call')
                FROM ({ROLLUP})""",
            params,
        )
        servers, tools, calls, errors = totals[0] if totals else (0, 0, 0, 0)

        breakdown = self._breakdown(
            self._rows(
                f"""SELECT failure_category, sum(calls) FROM ({ROLLUP})
                    WHERE failure_category != '' GROUP BY failure_category""",
                params,
            )
        )

        # How much of our error intelligence is real, versus coarse span-derived
        # guesswork. Surfaced because mixing the two silently would misrepresent
        # the product's core claim (D21).
        classified = self._rows(
            f"""SELECT sum(helper_classified), sum(calls) FROM ({ROLLUP})
                WHERE failure_category NOT IN ('', 'ok', 'protocol_error')""",
            params,
        )
        helper, total = classified[0] if classified else (0, 0)

        # Freshness uses a FIXED recent window, deliberately ignoring the
        # user's selected range. It answers "is the pipeline healthy right
        # now?", not "what was it historically" -- and a wide range sweeps in
        # REPLAYED spans, whose event time precedes their ingest time by hours
        # by design (D26). Over 24h that reported 19,820s of "latency", which
        # was measuring a replay, not the pipeline.
        #
        # Stays on `spans_raw`: `ingested_at` is a property of a span's journey
        # through the pipeline, and a minute bucket has already thrown away the
        # per-span arrival times this measures.
        freshness = self._rows(
            f"""SELECT quantile(0.95)(dateDiff('millisecond', timestamp, ingested_at))
                FROM spans_raw
                WHERE tenant_id = {{tenant:String}} AND project_id = {{project:String}}
                  AND timestamp >= now() - INTERVAL {FRESHNESS_WINDOW_MINUTES} MINUTE""",
            params,
        )

        return Overview(
            window_minutes=window,
            servers=servers,
            tools=tools,
            calls=calls,
            errors=errors or 0,
            failure_breakdown=breakdown,
            latency=self._rollup_latency(params, "AND mcp_method = 'tools/call'"),
            classified_ratio=round(helper / total, 3) if total else 1.0,
            freshness_p95_seconds=round(_number(freshness[0][0]) / 1000, 2) if freshness else 0.0,
        )

    def servers(self, tenant: str, project: str, since: datetime) -> list[ServerSummary]:
        params = self._scope(tenant, project, since)
        # Also the rollup. `uniqExact` over tool names is EXACT here rather than
        # an estimate, because the tool name is part of the rollup's sort key --
        # which is why one table with the tool in the key replaced the two the
        # register named (see 012_rollups.sql).
        rows = self._rows(
            f"""SELECT service_name, anyLast(service_version), anyLast(environment),
                       sumIf(calls, mcp_method = 'tools/call'),
                       sumIf(errors, mcp_method = 'tools/call'),
                       uniqExactIf(mcp_tool_name, mcp_tool_name != ''), max(last_seen)
                FROM ({ROLLUP}) WHERE service_name != ''
                GROUP BY service_name ORDER BY 4 DESC""",
            params,
        )
        out = []
        for name, version, env, calls, errors, tools, last in rows:
            breakdown = self._breakdown(
                self._rows(
                    f"""SELECT failure_category, sum(calls) FROM ({ROLLUP})
                        WHERE service_name = {{server:String}} AND failure_category != ''
                        GROUP BY failure_category""",
                    {**params, "server": name},
                )
            )
            out.append(
                ServerSummary(
                    server=name,
                    version=version or "",
                    environment=env or "",
                    calls=calls,
                    errors=errors or 0,
                    tools=tools,
                    failure_breakdown=breakdown,
                    latency=self._rollup_latency(
                        params,
                        "AND mcp_method = 'tools/call' AND service_name = {server:String}",
                        server=name,
                    ),
                    last_seen=last,
                )
            )
        return out

    def tools(
        self, tenant: str, project: str, since: datetime, server: str | None = None
    ) -> list[ToolSummary]:
        params = self._scope(tenant, project, since)
        filt = "AND service_name = {server:String}" if server else ""
        if server:
            params["server"] = server

        rows = self._rows(
            f"""SELECT mcp_tool_name, anyLast(service_name), count(),
                       sum(mcp_is_error), max(span_time)
                FROM ({LATEST_SPANS})
                WHERE mcp_method = 'tools/call' AND mcp_tool_name != '' {filt}
                GROUP BY mcp_tool_name ORDER BY 3 DESC""",
            params,
        )
        out = []
        for tool, svc, calls, errors, last in rows:
            breakdown = self._breakdown(
                self._rows(
                    f"""SELECT failure_category, count() FROM ({LATEST_SPANS})
                        WHERE mcp_tool_name = {{tool:String}} AND failure_category != ''
                        GROUP BY failure_category""",
                    {**params, "tool": tool},
                )
            )
            out.append(
                ToolSummary(
                    tool=tool,
                    server=svc or "",
                    calls=calls,
                    errors=errors or 0,
                    failure_breakdown=breakdown,
                    latency=self._latency_for(
                        params, "AND mcp_tool_name = {tool:String}", tool=tool
                    ),
                    last_seen=last,
                )
            )
        return out

    def capabilities(
        self,
        tenant: str,
        project: str,
        since: datetime,
        kind: str = "tool",
        filters: Filters | None = None,
    ) -> CapabilityPage:
        """Tools, prompts, resources or protocol methods -- one query path.

        They differ only in which `mcp_method` they filter and which column
        names the item. Four near-identical methods would drift apart; this one
        cannot.
        """
        method, name_col = CAPABILITY_KINDS.get(kind, CAPABILITY_KINDS["tool"])
        filters = filters or Filters("capabilities")
        params = self._scope(tenant, project, since)

        clauses = [f"{name_col} != ''"]
        if method:
            clauses.append("mcp_method = {method:String}")
            params["method"] = method
        else:
            # protocol = everything that is not a capability invocation
            known = ", ".join(f"'{m}'" for m, _ in CAPABILITY_KINDS.values() if m)
            clauses.append(f"mcp_method NOT IN ({known})")
        clauses.extend(filters.clauses(WHERE, params, name_col))
        where = " AND ".join(clauses)

        # HAVING, not a Python filter over the result: dropping rows after the
        # fact means the per-row latency queries below run for rows nobody
        # asked for -- one wasted ClickHouse round trip each.
        having = filters.clauses(HAVING, params, name_col)
        having_sql = (" HAVING " + " AND ".join(having)) if having else ""

        # `sort` is looked up in a fixed map and never taken from the query
        # string: ClickHouse cannot parameterise an identifier, so an
        # unvalidated sort key would be raw SQL from a URL.
        order = filters.order_by()

        # One extra row is fetched so truncation can be DETECTED rather than
        # guessed: exactly `cap` rows is ambiguous between "that is all of them"
        # and "there are more", and a table that silently drops the difference
        # is one someone will read as complete.
        params["cap"] = CAPABILITY_ROW_CAP + 1
        rows = self._rows(
            f"""SELECT {name_col} AS item, anyLast(service_name), anyLast(mcp_method),
                       count() AS calls, sum(mcp_is_error) AS errors,
                       max(span_time) AS last_seen,
                       quantileIf(0.95)(duration_ns / 1e6, is_latency_eligible = 1) AS p95_sort
                FROM ({LATEST_SPANS}) WHERE {where}
                GROUP BY item{having_sql} ORDER BY {order} LIMIT {{cap:UInt32}}""",
            params,
        )
        truncated = len(rows) > CAPABILITY_ROW_CAP
        rows = rows[:CAPABILITY_ROW_CAP]
        if not rows:
            return CapabilityPage(items=[], truncated=False, cap=CAPABILITY_ROW_CAP)

        # THE 2N+1. This used to run a breakdown query AND a latency query for
        # every row, so a tenant with 500 tools issued 1001 ClickHouse queries
        # for one page -- around 16ms each, measured, which crosses the 20s
        # max_execution_time somewhere past a thousand capabilities. Both are
        # now single grouped queries over the same row set, so the cost is three
        # queries whether the table has four rows or two hundred.
        names = [r[0] for r in rows]
        params["items"] = names
        item_filter = f"AND {name_col} IN {{items:Array(String)}}"

        # `where` is re-applied to BOTH follow-ups. The per-row versions omitted
        # it, so a breakdown counted every span sharing the item's name across
        # all servers and methods while `calls` beside it counted only the
        # filtered ones -- a row whose bar could sum to more than its own total.
        # Latent on data where no two servers share a tool name, wrong the
        # moment two do.
        breakdowns: dict[str, FailureBreakdown] = {}
        grouped: dict[str, list[Sequence[Any]]] = {name: [] for name in names}
        for item, category, count in self._rows(
            f"""SELECT {name_col} AS item, failure_category, count()
                FROM ({LATEST_SPANS})
                WHERE {where} AND failure_category != '' {item_filter}
                GROUP BY item, failure_category""",
            params,
        ):
            grouped.setdefault(item, []).append((category, count))
        for name in names:
            breakdowns[name] = self._breakdown(grouped.get(name, []))

        latencies = self._latencies_by_item(where, name_col, item_filter, params)

        # `p95_sort` exists only to make ORDER BY p95 possible in one pass; the
        # p95 actually REPORTED comes from the grouped latency query, which
        # applies the eligibility rule and the clock caveat. Two different
        # numbers would be a bug, so the sort key is never displayed.
        items = [
            CapabilityRow(
                kind=kind,
                name=name,
                method=meth or method,
                server=svc or "",
                calls=calls,
                errors=errors or 0,
                failure_breakdown=breakdowns[name],
                latency=latencies.get(name, LatencyStats()),
                last_seen=last,
            )
            for name, svc, meth, calls, errors, last, _p95_sort in rows
        ]
        return CapabilityPage(items=items, truncated=truncated, cap=CAPABILITY_ROW_CAP)

    def _latencies_by_item(
        self, where: str, name_col: str, item_filter: str, params: dict[str, Any]
    ) -> dict[str, LatencyStats]:
        """Latency for every item in one query, keyed by item.

        Column order matches `_LATENCY_SELECT` exactly so `_latency` -- which
        owns the unit conversion, the zero-duration count and the clock caveat
        -- stays the single place those are computed. Reimplementing that
        arithmetic here is how the grouped path would start disagreeing with the
        per-row one it replaced.
        """
        out: dict[str, LatencyStats] = {}
        for row in self._rows(
            f"""SELECT {name_col} AS item, count(), quantile(0.50)(duration_ns),
                       quantile(0.95)(duration_ns), quantile(0.99)(duration_ns),
                       max(duration_ns), countIf(duration_ns = 0),
                       min(if(duration_ns > 0, duration_ns, NULL))
                FROM ({LATEST_SPANS})
                WHERE {where} AND is_latency_eligible = 1 {item_filter}
                GROUP BY item""",
            params,
        ):
            out[row[0]] = self._latency(row[1:])
        return out

    def filter_options(self, tenant: str, project: str, since: datetime) -> FilterOptions:
        """The distinct values worth offering in a filter dropdown.

        From the data, not a constant. A hardcoded server list goes stale the
        first time somebody renames a service, and a dropdown offering a value
        that returns nothing is worse than no dropdown -- it looks like the
        filter is broken.

        One query, four columns, capped. The cap matters: a tenant with 10k
        distinct tools must not ship 10k options into a `<select>`, and the
        search box already covers the long tail.
        """
        params = self._scope(tenant, project, since)
        rows = self._rows(
            f"""SELECT
                    arraySlice(arraySort(groupUniqArrayIf(200)(service_name,
                        service_name != '')), 1, 200),
                    arraySlice(arraySort(groupUniqArrayIf(200)(mcp_tool_name,
                        mcp_tool_name != '')), 1, 200),
                    arraySlice(arraySort(groupUniqArrayIf(100)(mcp_method,
                        mcp_method != '')), 1, 100),
                    arraySlice(arraySort(groupUniqArrayIf(50)(failure_category,
                        failure_category NOT IN ('', 'ok'))), 1, 50)
                FROM ({LATEST_SPANS})""",
            params,
        )
        if not rows:
            return FilterOptions()
        servers, tools, methods, categories = rows[0]
        return FilterOptions(
            servers=list(servers),
            tools=list(tools),
            methods=list(methods),
            categories=list(categories),
        )

    def traces(
        self,
        tenant: str,
        project: str,
        since: datetime,
        limit: int = 50,
        cursor: str | None = None,
        filters: Filters | None = None,
        failures_only: bool = False,
    ) -> Page:
        params = self._scope(tenant, project, since)
        clauses = []
        if failures_only:
            # Filtered in SQL, NOT after pagination. Post-filtering a page means
            # a window whose most recent traces all succeeded returns an EMPTY
            # error list while errors sit just past the page boundary -- the
            # error view looking healthy is the worst possible lie for it to
            # tell. `pending_input` is excluded here because an MRTR interim
            # round is not a failure (D20).
            clauses.append(f"failure_category NOT IN ({_NOT_A_FAILURE_SQL})")
        if filters is not None:
            clauses.extend(filters.clauses(WHERE, params))
        if cursor:
            # Keyset, not OFFSET: a deep OFFSET re-scans everything before it,
            # and the page shifts under you as new spans arrive (V2 Â§13.1).
            cursor_time, cursor_trace_id = decode_cursor(cursor)
            clauses.append(
                "(start_time < {cursor_time:DateTime64(9)} OR "
                "(start_time = {cursor_time:DateTime64(9)} "
                "AND trace_id < {cursor_trace_id:String}))"
            )
            params["cursor_time"] = cursor_time
            params["cursor_trace_id"] = cursor_trace_id
        params["limit"] = limit + 1

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._rows(
            f"""SELECT trace_id, service_name, mcp_tool_name, mcp_method, start_time,
                       duration_ms, span_count, error_count, failure_category, transport
                FROM (
                    SELECT trace_id,
                           anyLastIf(service_name, service_name != '')   AS service_name,
                           anyLastIf(mcp_tool_name, mcp_tool_name != '') AS mcp_tool_name,
                           anyLastIf(mcp_method, mcp_method != '')       AS mcp_method,
                           anyLastIf(transport, transport != '')         AS transport,
                           min(span_time)                                AS start_time,
                           (toUnixTimestamp64Nano(max(span_time)) + max(duration_ns)
                            - toUnixTimestamp64Nano(min(span_time))) / 1e6 AS duration_ms,
                           count()                                       AS span_count,
                           sum(mcp_is_error)                             AS error_count,
                           argMax(failure_category, mcp_is_error)        AS failure_category
                    FROM ({LATEST_SPANS})
                    GROUP BY trace_id
                ) {where}
                ORDER BY start_time DESC, trace_id DESC LIMIT {{limit:UInt32}}""",
            params,
        )

        items = [
            TraceSummary(
                trace_id=r[0],
                server=r[1] or "",
                tool=r[2] or "",
                mcp_method=r[3] or "",
                start_time=r[4],
                duration_ms=round(_number(r[5]), 3),
                span_count=r[6],
                error_count=r[7] or 0,
                failure_category=r[8] or "",
                transport=r[9] or "",
            )
            for r in rows
        ]
        next_cursor = (
            encode_cursor(items[limit - 1].start_time, items[limit - 1].trace_id)
            if len(items) > limit
            else None
        )
        return Page(items=items[:limit], next_cursor=next_cursor)

    #: Every stored column, so span detail is complete by construction rather
    #: than by whoever last remembered to add a field (assertion D1).
    _SPAN_COLUMNS = (
        "span_id", "parent_span_id", "span_name", "span_kind", "timestamp",
        "duration_ns", "status_code", "status_message", "service_name",
        "service_version", "deployment_environment", "mcp_method",
        "mcp_tool_name", "mcp_prompt_name", "mcp_resource_uri",
        "gen_ai_operation", "protocol_version", "jsonrpc_request_id",
        "transport", "mcp_session_id", "mcp_is_error", "result_type",
        "failure_category", "failure_detail", "failure_kind_source",
        "classifier_version", "error_type", "rpc_status_code",
        "is_latency_eligible", "mrtr_state_in", "mrtr_state_out",
        "downstream_kind", "http_method", "http_status_code", "http_host",
        "db_system", "db_operation", "db_collection", "http_url",
        "messaging_system", "messaging_destination", "messaging_operation",
        "db_statement", "gen_ai_system",
        "http_request_body",
        "http_request_headers", "http_response_headers",
        "client_name", "client_version",
        "gen_ai_model", "gen_ai_input_tokens", "gen_ai_output_tokens",
        "input_size", "output_size", "input_preview", "output_preview",
        "span_attributes", "resource_attributes", "normalization_version",
        "kafka_partition", "kafka_offset", "ingested_at",
    )

    def trace(self, tenant: str, project: str, trace_id: str) -> TraceDetail | None:
        # trace_locator turns this into a point lookup instead of a scan of
        # spans_raw, which is ordered by (tenant, project, time, ...).
        # LIMIT 1 BY is mandatory: ReplacingMergeTree dedupes only on merge, and
        # a replay re-inserts every trace_id (D25).
        located = self._rows(
            """SELECT trace_date FROM trace_locator
               WHERE trace_id = {trace:String} AND tenant_id = {tenant:String}
                 AND project_id = {project:String}
               ORDER BY first_seen DESC LIMIT 1 BY trace_id""",
            {"trace": trace_id, "tenant": tenant, "project": project},
        )
        if not located:
            return None

        # argMax over normalization_version per span (D24): a replay leaves
        # several versions and a naive read mixes corrected rows with buggy ones.
        # Aliases must not shadow a source column used in the WHERE clause:
        # `argMax(timestamp, ...) AS timestamp` makes ClickHouse resolve the
        # `toDate(timestamp)` filter to the aggregate and reject the query with
        # ILLEGAL_AGGREGATION. Only the alias changes; rows are read positionally
        # against _SPAN_COLUMNS, so the dict keys are unaffected.
        # EVERY alias is prefixed, so no alias can shadow a source column.
        # Unprefixed aliases bit three times here: `AS timestamp` made the WHERE
        # resolve to the aggregate, and `AS normalization_version` made the
        # ranking column inside every other argMax resolve to an aggregate.
        # Both surface as ILLEGAL_AGGREGATION with a confusing message. Rows are
        # read positionally against _SPAN_COLUMNS, so aliases are cosmetic and
        # prefixing costs nothing.
        def pick(column: str) -> str:
            if column == "normalization_version":
                # Cannot argMax a column by itself; max() is the same answer.
                return "max(normalization_version) AS s_normalization_version"
            return f"argMax({column}, normalization_version) AS s_{column}"

        selects = ", ".join(pick(c) for c in self._SPAN_COLUMNS if c != "span_id")
        rows = self._rows(
            f"""SELECT span_id, {selects}
                FROM spans_raw
                WHERE trace_id = {{trace:String}} AND tenant_id = {{tenant:String}}
                  AND project_id = {{project:String}} AND toDate(timestamp) = {{day:Date}}
                GROUP BY span_id
                ORDER BY s_timestamp ASC
                LIMIT {{cap:UInt32}}""",
            {
                "trace": trace_id,
                "tenant": tenant,
                "project": project,
                "day": located[0][0],
                # One past the cap, so truncation is DETECTED rather than
                # guessed: exactly `cap` rows is ambiguous between "that is the
                # whole trace" and "there is more".
                "cap": TRACE_SPAN_CAP + 1,
            },
        )
        if not rows:
            return None

        raw = [dict(zip(self._SPAN_COLUMNS, row, strict=True)) for row in rows]
        raw.sort(key=lambda item: item["timestamp"])
        # Ordered by time in SQL, so what is dropped is the TAIL of the trace
        # rather than an arbitrary slice. A truncated waterfall still reads
        # forwards from the start of the call.
        truncated = len(raw) > TRACE_SPAN_CAP
        raw = raw[:TRACE_SPAN_CAP]

        trace_start = raw[0]["timestamp"]
        trace_end_ms = max(
            item["timestamp"].timestamp() * 1000 + (item["duration_ns"] or 0) / 1e6
            for item in raw
        )
        total_ms = round(trace_end_ms - trace_start.timestamp() * 1000, 3)

        # Self-time: total minus the sum of direct children. Computed here
        # because it needs the whole trace, and it is the difference between
        # "this tool is slow" and "this tool waits on something slow".
        child_ms: dict[str, float] = {}
        for item in raw:
            parent = item["parent_span_id"] or ""
            if parent:
                child_ms[parent] = child_ms.get(parent, 0.0) + (item["duration_ns"] or 0) / 1e6

        spans: list[SpanDTO] = []
        detail: dict[str, SpanDetail] = {}
        for item in raw:
            duration_ms = round((item["duration_ns"] or 0) / 1e6, 3)
            self_ms = round(max(0.0, duration_ms - child_ms.get(item["span_id"], 0.0)), 3)
            offset_ms = round(
                (item["timestamp"].timestamp() - trace_start.timestamp()) * 1000, 3
            )
            downstream_detail = _detail(
                item["downstream_kind"], item["http_method"], item["http_status_code"],
                item["db_system"], item["gen_ai_model"], item["db_operation"],
                item["gen_ai_input_tokens"], item["gen_ai_output_tokens"],
            )
            spans.append(
                SpanDTO(
                    span_id=item["span_id"],
                    parent_span_id=item["parent_span_id"] or "",
                    name=item["span_name"],
                    kind=item["span_kind"] or "",
                    start_time=item["timestamp"],
                    duration_ms=duration_ms,
                    self_ms=self_ms,
                    offset_ms=offset_ms,
                    status=item["status_code"] or "UNSET",
                    failure_category=item["failure_category"] or "",
                    failure_detail=item["failure_detail"] or "",
                    mcp_method=item["mcp_method"] or "",
                    tool=item["mcp_tool_name"] or "",
                    downstream_kind=item["downstream_kind"] or "",
                    downstream_detail=downstream_detail,
                    is_latency_eligible=bool(item["is_latency_eligible"]),
                )
            )
            ingested = item["ingested_at"]
            detail[item["span_id"]] = SpanDetail(
                span_id=item["span_id"],
                parent_span_id=item["parent_span_id"] or "",
                trace_id=trace_id,
                name=item["span_name"],
                kind=item["span_kind"] or "",
                service_name=item["service_name"] or "",
                service_version=item["service_version"] or "",
                environment=item["deployment_environment"] or "",
                service_instance=(item["resource_attributes"] or {}).get(
                    "service.instance.id", ""
                ),
                start_time=item["timestamp"],
                duration_ms=duration_ms,
                self_ms=self_ms,
                offset_ms=offset_ms,
                pct_of_trace=round(100 * duration_ms / total_ms, 1) if total_ms else 0.0,
                status=item["status_code"] or "UNSET",
                status_message=item["status_message"] or "",
                failure_category=item["failure_category"] or "",
                failure_detail=item["failure_detail"] or "",
                failure_kind_source=item["failure_kind_source"] or "",
                classifier_version=int(_number(item["classifier_version"])),
                error_type=item["error_type"] or "",
                rpc_status_code=item["rpc_status_code"],
                is_error=bool(item["mcp_is_error"]),
                mcp_method=item["mcp_method"] or "",
                tool=item["mcp_tool_name"] or "",
                prompt=item["mcp_prompt_name"] or "",
                resource_uri=item["mcp_resource_uri"] or "",
                gen_ai_operation=item["gen_ai_operation"] or "",
                protocol_version=item["protocol_version"] or "",
                jsonrpc_request_id=item["jsonrpc_request_id"] or "",
                transport=item["transport"] or "",
                session_id=item["mcp_session_id"],
                result_type=item["result_type"] or "",
                mrtr_state_in=item["mrtr_state_in"] or "",
                mrtr_state_out=item["mrtr_state_out"] or "",
                is_latency_eligible=bool(item["is_latency_eligible"]),
                downstream_kind=item["downstream_kind"] or "",
                http_method=item["http_method"] or "",
                http_status_code=item["http_status_code"],
                http_host=item["http_host"] or "",
                db_system=item["db_system"] or "",
                db_operation=item["db_operation"] or "",
                db_collection=item["db_collection"] or "",
                messaging_system=item["messaging_system"] or "",
                messaging_destination=item["messaging_destination"] or "",
                messaging_operation=item["messaging_operation"] or "",
                http_url=item["http_url"] or "",
                db_statement=item["db_statement"] or "",
                http_request_body=item["http_request_body"] or "",
                http_request_headers=item["http_request_headers"] or "",
                http_response_headers=item["http_response_headers"] or "",
                client_name=item["client_name"] or "",
                client_version=item["client_version"] or "",
                gen_ai_system=item["gen_ai_system"] or "",
                gen_ai_model=item["gen_ai_model"] or "",
                gen_ai_input_tokens=item["gen_ai_input_tokens"],
                gen_ai_output_tokens=item["gen_ai_output_tokens"],
                input_size=item["input_size"],
                output_size=item["output_size"],
                input_preview=item["input_preview"],
                output_preview=item["output_preview"],
                span_attributes=item["span_attributes"] or {},
                resource_attributes=item["resource_attributes"] or {},
                normalization_version=int(_number(item["normalization_version"])),
                kafka_partition=int(_number(item["kafka_partition"])),
                kafka_offset=int(_number(item["kafka_offset"])),
                ingested_at=ingested,
                freshness_ms=round(
                    (ingested.timestamp() - item["timestamp"].timestamp()) * 1000, 1
                )
                if ingested
                else 0.0,
            )

        root = _resolve_root(spans)
        # Reassigned, because the ordering IS the tree: a waterfall that returns
        # spans in timestamp order can put a child above its parent.
        spans = _order_tree(spans, root)
        for span in spans:
            detail[span.span_id].depth = span.depth

        headline = _headline(spans)
        # Detail is dropped for size, not for correctness: every span is still
        # returned and the waterfall is complete. Only the per-span field maps
        # go, and the console fetches them on demand instead.
        detail_omitted = len(spans) > TRACE_DETAIL_CAP
        if detail_omitted:
            detail = {}
        return TraceDetail(
            trace_id=trace_id,
            server=raw[0]["service_name"] or "",
            # BOTH from the SAME span. Picked independently, they described
            # different operations: a trace carrying `tools/list` and
            # `tools/call slow_export` rendered a header reading "slow_export"
            # with "METHOD tools/list" beside it -- a method that tool was never
            # called by. The header names ONE operation, so it takes one span.
            #
            # A span with a tool wins, because that is the work the trace is
            # about; `tools/list` is the client's handshake around it.
            tool=headline.tool if headline else "",
            mcp_method=headline.mcp_method if headline else "",
            start_time=trace_start,
            duration_ms=total_ms,
            span_count=len(spans),
            error_count=sum(
                1 for s in spans if s.failure_category not in ("", "ok", "pending_input")
            ),
            failure_category=next(
                (s.failure_category for s in spans if s.failure_category not in ("", "ok")),
                "ok",
            ),
            root_span_id=root.span_id if root else "",
            spans=spans,
            detail=detail,
            truncated=truncated,
            span_cap=TRACE_SPAN_CAP,
            detail_omitted=detail_omitted,
            detail_cap=TRACE_DETAIL_CAP,
        )


def _detail(
    kind: str,
    http_method: str,
    http_status: Any,
    db: str,
    model: str,
    db_operation: str = "",
    in_tokens: Any = None,
    out_tokens: Any = None,
) -> str:
    """One line saying what this span actually did.

    Rendered inline in the waterfall so "where did the time go" is answerable
    without opening every span in turn.
    """
    if kind == "http":
        return f"{http_method} {http_status}".strip() if http_status else (http_method or "")
    if kind == "db":
        return " ".join(part for part in (db, db_operation) if part)
    if kind == "llm":
        tokens = f"{in_tokens}\u2192{out_tokens} tok" if in_tokens or out_tokens else ""
        return " ".join(part for part in (model, tokens) if part)
    return ""


def _resolve_root(spans: list[SpanDTO]) -> SpanDTO | None:
    """The span whose parent is absent from this trace.

    NOT simply `parent_span_id == ''`: per D7 an instrumented client makes the
    MCP span a legitimate child, so the trace's root is whichever span's parent
    we never received. Resolvable here because the whole trace is in hand --
    which is exactly why it is not computed in the incremental summary (D22).
    """
    present = {s.span_id for s in spans}
    orphans = [s for s in spans if not s.parent_span_id or s.parent_span_id not in present]
    return min(orphans, key=lambda s: s.start_time) if orphans else None


def _headline(spans: list[SpanDTO]) -> SpanDTO | None:
    """The span a trace's header should describe.

    A tool call if there is one -- that is the work the trace is about, and
    `tools/list` is the handshake the client wraps around it. Otherwise the
    first span carrying any MCP method.
    """
    return (
        next((s for s in spans if s.tool), None)
        or next((s for s in spans if s.mcp_method), None)
    )


def _order_tree(spans: list[SpanDTO], root: SpanDTO | None) -> list[SpanDTO]:
    """Depth-first order with depths: parents before children, siblings by time.

    THE BUG THIS FIXES
        Depths were computed correctly and the list was still returned in
        TIMESTAMP order, so a child could be rendered before its own parent.
        Measured on a real trace: `tools/list` (depth 1) and `POST /mcp`
        (depth 0, its parent) start in the same millisecond, so the child sorted
        first and the waterfall drew

            tools/list                 <- depth 1, no parent above it
            POST /mcp          ROOT
              tools/call
                mcp.progress

        which reads as "tools/list is a root and POST /mcp is something else".
        The indentation was right and the ORDER made it a lie -- and indentation
        is only meaningful if the row above a child is its ancestor.

        Timestamp order is not a near-miss for tree order, either. Architecture
        U2 says a child routinely lands before its parent, so this misrenders
        whenever two spans share a start time OR a parent starts late -- both
        normal, neither rare.

    Siblings are still ordered by start time, which is what makes a waterfall
    readable within one level.
    """
    by_parent: dict[str, list[SpanDTO]] = {}
    for span in spans:
        by_parent.setdefault(span.parent_span_id, []).append(span)
    for children in by_parent.values():
        children.sort(key=lambda s: s.start_time)

    ordered: list[SpanDTO] = []
    seen: set[str] = set()
    if root is not None:
        stack = [(root, 0)]
        while stack:
            span, depth = stack.pop()
            if span.span_id in seen:  # cycle guard: telemetry is untrusted input
                continue
            seen.add(span.span_id)
            span.depth = depth
            ordered.append(span)
            # Reversed, because a stack pops last-first and siblings must come
            # out in the start-time order they were just sorted into.
            stack.extend(
                (child, depth + 1)
                for child in reversed(by_parent.get(span.span_id, []))
            )

    # Anything unreachable from the root. A parent that has not arrived yet is
    # NORMAL, not an error (Architecture U2), so these are appended in time
    # order rather than dropped -- a span missing from the waterfall is worse
    # than one drawn at the wrong indent.
    leftover = [span for span in spans if span.span_id not in seen]
    leftover.sort(key=lambda s: s.start_time)
    ordered.extend(leftover)
    return ordered


def encode_cursor(value: datetime, trace_id: str) -> str:
    """Encode the complete keyset position.

    A timestamp alone is not a total order: when a page ends in the middle of
    several traces with the same start time, the remaining ties would vanish
    from the next page. The trace id is the deterministic second key used by
    both ORDER BY and the seek predicate.
    """
    payload = json.dumps([value.isoformat(), trace_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode an opaque cursor, normalizing every malformed shape to ValueError."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not isinstance(raw[0], str)
            or not isinstance(raw[1], str)
            or not raw[1]
        ):
            raise ValueError("invalid cursor")
        return datetime.fromisoformat(raw[0]), raw[1]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
