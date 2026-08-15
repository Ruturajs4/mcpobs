"""ClickHouse reads.

THREE RULES, ENFORCED HERE SO NO ENDPOINT CAN FORGET THEM
    1. Tenant scoping is applied by this layer, never by a caller. An endpoint
       that forgets a `WHERE tenant_id` leaks another customer's telemetry, and
       that must not be possible to write by accident (V2 §13.1).
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
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from query.dtos import (
    FailureBreakdown,
    LatencyStats,
    Overview,
    Page,
    ServerSummary,
    SpanDTO,
    ToolSummary,
    TraceDetail,
    TraceSummary,
)

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
    argMax(mcp_is_error, normalization_version)        AS mcp_is_error,
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


FRESHNESS_WINDOW_MINUTES = 15
"""Freshness is a "right now" health signal, never a historical aggregate."""

CATEGORIES = (
    "ok",
    "tool_error",
    "server_exception",
    "unknown_tool",
    "invalid_arguments",
    "protocol_error",
    "pending_input",
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
    def _latency(row: Sequence[Any] | None) -> LatencyStats:
        if not row or not row[0]:
            return LatencyStats()
        count, p50, p95, p99, mx, zeros = row
        return LatencyStats(
            count=count,
            p50_ms=round(_number(p50) / 1e6, 3),
            p95_ms=round(_number(p95) / 1e6, 3),
            p99_ms=round(_number(p99) / 1e6, 3),
            max_ms=round(_number(mx) / 1e6, 3),
            zero_duration=int(_number(zeros)),
        )

    _LATENCY_SELECT = """
        SELECT count(), quantile(0.50)(duration_ns), quantile(0.95)(duration_ns),
               quantile(0.99)(duration_ns), max(duration_ns), countIf(duration_ns = 0)
        FROM ({latest}) WHERE is_latency_eligible = 1 AND mcp_method = 'tools/call' {extra}
    """

    def _latency_for(
        self, params: dict[str, Any], extra: str = "", **extra_params: Any
    ) -> LatencyStats:
        sql = self._LATENCY_SELECT.format(latest=LATEST_SPANS, extra=extra)
        rows = self._rows(sql, {**params, **extra_params})
        return self._latency(rows[0] if rows else None)

    # -- reads -------------------------------------------------------------
    def overview(self, tenant: str, project: str, since: datetime, window: int) -> Overview:
        params = self._scope(tenant, project, since)

        totals = self._rows(
            f"""SELECT uniqExact(service_name), uniqExactIf(mcp_tool_name, mcp_tool_name != ''),
                       countIf(mcp_method = 'tools/call'),
                       sumIf(mcp_is_error, mcp_method = 'tools/call')
                FROM ({LATEST_SPANS})""",
            params,
        )
        servers, tools, calls, errors = totals[0] if totals else (0, 0, 0, 0)

        breakdown = self._breakdown(
            self._rows(
                f"""SELECT failure_category, count() FROM ({LATEST_SPANS})
                    WHERE failure_category != '' GROUP BY failure_category""",
                params,
            )
        )

        # How much of our error intelligence is real, versus coarse span-derived
        # guesswork. Surfaced because mixing the two silently would misrepresent
        # the product's core claim (D21).
        classified = self._rows(
            f"""SELECT countIf(failure_kind_source = 'helper'), count()
                FROM ({LATEST_SPANS})
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
            latency=self._latency_for(params),
            classified_ratio=round(helper / total, 3) if total else 1.0,
            freshness_p95_seconds=round(_number(freshness[0][0]) / 1000, 2) if freshness else 0.0,
        )

    def servers(self, tenant: str, project: str, since: datetime) -> list[ServerSummary]:
        params = self._scope(tenant, project, since)
        rows = self._rows(
            f"""SELECT service_name, anyLast(service_version),
                       anyLast(environment), countIf(mcp_method = 'tools/call'),
                       sumIf(mcp_is_error, mcp_method = 'tools/call'),
                       uniqExactIf(mcp_tool_name, mcp_tool_name != ''), max(span_time)
                FROM ({LATEST_SPANS}) WHERE service_name != ''
                GROUP BY service_name ORDER BY 4 DESC""",
            params,
        )
        out = []
        for name, version, env, calls, errors, tools, last in rows:
            breakdown = self._breakdown(
                self._rows(
                    f"""SELECT failure_category, count() FROM ({LATEST_SPANS})
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
                    latency=self._latency_for(
                        params, "AND service_name = {server:String}", server=name
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

    def traces(
        self,
        tenant: str,
        project: str,
        since: datetime,
        limit: int = 50,
        cursor: str | None = None,
        failure_category: str | None = None,
        tool: str | None = None,
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
            clauses.append("failure_category NOT IN ('', 'ok', 'pending_input')")
        if failure_category:
            clauses.append("failure_category = {category:String}")
            params["category"] = failure_category
        if tool:
            clauses.append("mcp_tool_name = {tool:String}")
            params["tool"] = tool
        if cursor:
            # Keyset, not OFFSET: a deep OFFSET re-scans everything before it,
            # and the page shifts under you as new spans arrive (V2 §13.1).
            clauses.append("start_time < {cursor:DateTime64(9)}")
            params["cursor"] = decode_cursor(cursor)
        params["limit"] = limit + 1

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._rows(
            f"""SELECT trace_id, service_name, mcp_tool_name, mcp_method, start_time,
                       duration_ms, span_count, error_count, failure_category
                FROM (
                    SELECT trace_id,
                           anyLastIf(service_name, service_name != '')   AS service_name,
                           anyLastIf(mcp_tool_name, mcp_tool_name != '') AS mcp_tool_name,
                           anyLastIf(mcp_method, mcp_method != '')       AS mcp_method,
                           min(span_time)                                AS start_time,
                           (toUnixTimestamp64Nano(max(span_time)) + max(duration_ns)
                            - toUnixTimestamp64Nano(min(span_time))) / 1e6 AS duration_ms,
                           count()                                       AS span_count,
                           sum(mcp_is_error)                             AS error_count,
                           argMax(failure_category, mcp_is_error)        AS failure_category
                    FROM ({LATEST_SPANS})
                    GROUP BY trace_id
                ) {where}
                ORDER BY start_time DESC LIMIT {{limit:UInt32}}""",
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
            )
            for r in rows
        ]
        next_cursor = encode_cursor(items[limit - 1].start_time) if len(items) > limit else None
        return Page(items=items[:limit], next_cursor=next_cursor)

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
        trace_date = located[0][0]

        rows = self._rows(
            """SELECT span_id,
                      argMax(parent_span_id, normalization_version),
                      argMax(span_name, normalization_version),
                      argMax(span_kind, normalization_version),
                      argMax(timestamp, normalization_version),
                      argMax(duration_ns, normalization_version),
                      argMax(status_code, normalization_version),
                      argMax(failure_category, normalization_version),
                      argMax(mcp_method, normalization_version),
                      argMax(mcp_tool_name, normalization_version),
                      argMax(downstream_kind, normalization_version),
                      argMax(http_method, normalization_version),
                      argMax(http_status_code, normalization_version),
                      argMax(db_system, normalization_version),
                      argMax(gen_ai_model, normalization_version),
                      argMax(is_latency_eligible, normalization_version),
                      argMax(service_name, normalization_version)
               FROM spans_raw
               WHERE trace_id = {trace:String} AND tenant_id = {tenant:String}
                 AND project_id = {project:String} AND toDate(timestamp) = {day:Date}
               GROUP BY span_id ORDER BY 5""",
            {"trace": trace_id, "tenant": tenant, "project": project, "day": trace_date},
        )
        if not rows:
            return None

        spans = [
            SpanDTO(
                span_id=r[0],
                parent_span_id=r[1] or "",
                name=r[2],
                kind=r[3] or "",
                start_time=r[4],
                duration_ms=round(_number(r[5]) / 1e6, 3),
                status=r[6] or "UNSET",
                failure_category=r[7] or "",
                mcp_method=r[8] or "",
                tool=r[9] or "",
                downstream_kind=r[10] or "",
                downstream_detail=_detail(r[10], r[11], r[12], r[13], r[14]),
                is_latency_eligible=bool(r[15]),
            )
            for r in rows
        ]

        root = _resolve_root(spans)
        _assign_depths(spans, root)

        first, last_end = spans[0], max(
            (s.start_time.timestamp() * 1000 + s.duration_ms) for s in spans
        )
        return TraceDetail(
            trace_id=trace_id,
            server=rows[0][16] or "",
            tool=next((s.tool for s in spans if s.tool), ""),
            mcp_method=next((s.mcp_method for s in spans if s.mcp_method), ""),
            start_time=first.start_time,
            duration_ms=round(last_end - first.start_time.timestamp() * 1000, 3),
            span_count=len(spans),
            error_count=sum(
                1 for s in spans if s.failure_category not in ("", "ok", "pending_input")
            ),
            failure_category=next(
                (s.failure_category for s in spans if s.failure_category not in ("", "ok")), "ok"
            ),
            root_span_id=root.span_id if root else "",
            spans=spans,
        )


def _detail(kind: str, http_method: str, http_status: Any, db: str, model: str) -> str:
    if kind == "http":
        return f"{http_method} {http_status}".strip() if http_status else (http_method or "")
    if kind == "db":
        return db or ""
    if kind == "llm":
        return model or ""
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


def _assign_depths(spans: list[SpanDTO], root: SpanDTO | None) -> None:
    """Depth for waterfall indentation, without recursion."""
    if root is None:
        return
    by_parent: dict[str, list[SpanDTO]] = {}
    for span in spans:
        by_parent.setdefault(span.parent_span_id, []).append(span)

    stack = [(root, 0)]
    seen = set()
    while stack:
        span, depth = stack.pop()
        if span.span_id in seen:  # cycle guard: telemetry is untrusted input
            continue
        seen.add(span.span_id)
        span.depth = depth
        stack.extend((child, depth + 1) for child in by_parent.get(span.span_id, []))


def encode_cursor(value: datetime) -> str:
    return base64.urlsafe_b64encode(value.isoformat().encode()).decode()


def decode_cursor(cursor: str) -> datetime:
    return datetime.fromisoformat(base64.urlsafe_b64decode(cursor.encode()).decode())
