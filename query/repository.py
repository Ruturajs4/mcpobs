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
    CapabilityRow,
    FailureBreakdown,
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

    def capabilities(
        self,
        tenant: str,
        project: str,
        since: datetime,
        kind: str = "tool",
        server: str | None = None,
    ) -> list[CapabilityRow]:
        """Tools, prompts, resources or protocol methods -- one query path.

        They differ only in which `mcp_method` they filter and which column
        names the item. Four near-identical methods would drift apart; this one
        cannot.
        """
        method, name_col = CAPABILITY_KINDS.get(kind, CAPABILITY_KINDS["tool"])
        params = self._scope(tenant, project, since)

        clauses = [f"{name_col} != ''"]
        if method:
            clauses.append("mcp_method = {method:String}")
            params["method"] = method
        else:
            # protocol = everything that is not a capability invocation
            known = ", ".join(f"'{m}'" for m, _ in CAPABILITY_KINDS.values() if m)
            clauses.append(f"mcp_method NOT IN ({known})")
        if server:
            clauses.append("service_name = {server:String}")
            params["server"] = server
        where = " AND ".join(clauses)

        rows = self._rows(
            f"""SELECT {name_col} AS item, anyLast(service_name), anyLast(mcp_method),
                       count(), sum(mcp_is_error), max(span_time)
                FROM ({LATEST_SPANS}) WHERE {where}
                GROUP BY item ORDER BY 4 DESC""",
            params,
        )

        out = []
        for name, svc, meth, calls, errors, last in rows:
            scoped = {**params, "item": name}
            breakdown = self._breakdown(
                self._rows(
                    f"""SELECT failure_category, count() FROM ({LATEST_SPANS})
                        WHERE {name_col} = {{item:String}} AND failure_category != ''
                        GROUP BY failure_category""",
                    scoped,
                )
            )
            out.append(
                CapabilityRow(
                    kind=kind,
                    name=name,
                    method=meth or method,
                    server=svc or "",
                    calls=calls,
                    errors=errors or 0,
                    failure_breakdown=breakdown,
                    latency=self._latency_for(
                        params,
                        f"AND {name_col} = {{item:String}}",
                        item=name,
                        _any_method=True,
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
                GROUP BY span_id""",
            {"trace": trace_id, "tenant": tenant, "project": project, "day": located[0][0]},
        )
        if not rows:
            return None

        raw = [dict(zip(self._SPAN_COLUMNS, row, strict=True)) for row in rows]
        raw.sort(key=lambda item: item["timestamp"])

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
        _assign_depths(spans, root)
        for span in spans:
            detail[span.span_id].depth = span.depth

        return TraceDetail(
            trace_id=trace_id,
            server=raw[0]["service_name"] or "",
            tool=next((s.tool for s in spans if s.tool), ""),
            mcp_method=next((s.mcp_method for s in spans if s.mcp_method), ""),
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
