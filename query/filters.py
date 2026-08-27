"""What can be filtered, in ONE place.

Before this file, adding a single filter meant twelve edits across four files:
a field on the DTO, a line in `describe()`, a clause in the repository, a
parameter on two endpoints, and five separate places in the browser -- the empty
state, the query builder, the description, the dropdown switch and the URL
parser. Any one of them silently omitted gave a filter that half-worked, which
is precisely how `?server=` came to be accepted on /traces and ignored.

So the browser is not told about filters at all. It asks `/api/v1/filters?view=`
and renders whatever comes back: labels, control types, help text AND the option
values, all resolved server-side from the data actually present. The filter
panel is generic -- it has no knowledge of servers, tools, durations or
categories, and gains none when a filter is added.

ADDING A FILTER IS ONE ENTRY IN THIS FILE. Nothing in query/static, nothing on
the endpoints, nothing in the DTOs.

Two invariants hold for every filter here:

  * VALUES ARE ALWAYS BOUND. `{name:Type}` placeholders and a params dict, never
    interpolation. A server named `'; DROP` is a server name.
  * SQL FRAGMENTS ARE CONSTANTS. Where a filter needs more than a comparison
    (`status`, which maps to a set of categories), the fragment is written here
    and the user's value only selects WHICH constant. No user input ever becomes
    SQL structure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from query.dtos import NOT_A_FAILURE

# Control types the generic panel knows how to render. Adding a KIND is a
# frontend change; adding a FILTER of an existing kind is not.
SELECT = "select"  # one value from a list the backend supplies
SEARCH = "search"  # free text, matched across several columns
TOGGLE = "toggle"  # on/off
NUMBER = "number"  # numeric comparison

# Where in the query a filter lands. Capability tables aggregate, so "calls >= n"
# is a HAVING and "name contains x" is a WHERE -- the difference is not cosmetic:
# a HAVING in the WHERE would compare a column that does not exist yet.
WHERE = "where"
HAVING = "having"
SORT = "sort"


@dataclass(frozen=True)
class Filter:
    """One filter: how to render it, how to describe it, how to run it."""

    key: str
    label: str
    kind: str = SELECT
    #: Column to compare. `{name}` is substituted with the capability table's
    #: name column, which differs per kind (tool / prompt / resource).
    column: str = ""
    #: SEARCH matches any of these.
    columns: tuple[str, ...] = ()
    #: Attribute on FilterOptions supplying the choices, resolved per request so
    #: a dropdown never offers a server that stopped reporting.
    source: str = ""
    #: Fixed choices, for filters whose values are not data.
    choices: tuple[tuple[str, str], ...] = ()
    #: value -> SQL fragment, for filters that are not a plain comparison. The
    #: fragments are constants; the value only picks one.
    predicates: tuple[tuple[str, str], ...] = ()
    op: str = "="
    ch_type: str = "String"
    maximum: float | None = None
    stage: str = WHERE
    group: str = "Filter"
    help: str = ""
    placeholder: str = ""
    #: Offered as a field in the advanced query-builder rows.
    advanced: bool = False
    #: Shown in the compact bar rather than only inside the panel. Reserved for
    #: the one or two filters used constantly -- everything else would recreate
    #: the cramped bar the panel exists to replace.
    pinned: bool = False

    # ---------------------------------------------------------------- render
    def catalog(self, options: Any) -> dict[str, Any]:
        """The description the browser renders. No SQL, no column names.

        Columns are deliberately NOT sent: they are storage detail, and a
        frontend that knows them is a frontend that will eventually send one.
        """
        entry: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "group": self.group,
            "help": self.help,
            "placeholder": self.placeholder,
            "pinned": self.pinned,
            "show_chip": self.stage != SORT,
        }
        if self.kind in (SELECT,):
            entry["options"] = [{"value": v, "label": lbl} for v, lbl in self.resolve(options)]
        if self.kind == NUMBER:
            entry["minimum"] = 0
            if self.maximum is not None:
                entry["maximum"] = self.maximum
        return entry

    def resolve(self, options: Any) -> list[tuple[str, str]]:
        """Choices for this filter: fixed ones, or values present in the data."""
        if self.choices:
            return list(self.choices)
        if self.source and options is not None:
            return [(v, v) for v in getattr(options, self.source, []) or []]
        return []

    # ----------------------------------------------------------------- parse
    def parse(self, raw: str) -> Any:
        """Query-string text -> a value, or ValueError.

        Rejecting here rather than at the SQL boundary means a bad number is a
        400 with a field name, not a ClickHouse type error in a log.
        """
        if self.kind == TOGGLE:
            return raw.lower() in ("1", "true", "yes", "on")
        if self.kind == NUMBER:
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"{self.key} must be a number") from exc
            if value < 0:
                raise ValueError(f"{self.key} must not be negative")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.key} is too large")
            return int(value) if self.ch_type.startswith("UInt") else value
        text = raw.strip()
        if len(text) > 200:
            raise ValueError(f"{self.key} is too long")
        if self.kind == SELECT and self.choices and text:
            allowed = {v for v, _ in self.choices}
            if text not in allowed:
                # Dropped, not rejected: a stale bookmark from before a choice
                # was renamed should widen the result, not show an error page.
                return ""
        return text

    # ------------------------------------------------------------------- sql
    def clause(self, value: Any, params: dict[str, Any], name_column: str = "") -> str | None:
        """SQL fragment for this filter, binding `value` into `params`."""
        if value in (None, "", False):
            return None
        param = f"f_{self.key}"
        column = self.column.replace("{name}", name_column)

        if self.predicates:
            return dict(self.predicates).get(str(value))

        if self.kind == SEARCH:
            params[param] = value
            cols = [c.replace("{name}", name_column) for c in self.columns]
            return "(" + " OR ".join(
                f"positionCaseInsensitive({c}, {{{param}:String}}) > 0" for c in cols
            ) + ")"

        if self.kind == TOGGLE:
            # No parameter: the fragment is the whole meaning of "on".
            return column

        params[param] = value
        return f"{column} {self.op} {{{param}:{self.ch_type}}}"

#: NOT_A_FAILURE as a SQL list. Rendered from the constant rather than written
#: out, so `?status=error` cannot drift from the overview's error rate the way
#: it had: it counted 401s that the headline number excluded.
#:
#: Safe to interpolate ONLY because every element is a literal from our own
#: source -- no user input reaches this string.
_NOT_A_FAILURE_SQL = ", ".join(f"'{c}'" for c in NOT_A_FAILURE)


# ===========================================================================
# The filters themselves
# ===========================================================================
#
# Trace and error lists. Every clause here is applied to the AGGREGATED
# per-trace row, never to individual spans: filtering spans would drop spans
# from the traces it keeps, so a matching trace would come back with a
# truncated waterfall, a wrong span count and a duration measured across the
# survivors.

TRACE_FILTERS: tuple[Filter, ...] = (
    Filter(
        key="q", label="Search", kind=SEARCH, pinned=True,
        columns=("trace_id", "mcp_tool_name", "service_name", "mcp_method"),
        placeholder="Search trace id, tool, server, method…",
        help="Case-insensitive substring across trace id, tool, server and method.",
        group="Search",
    ),
    Filter(
        key="server", label="Server", column="service_name", source="servers",
        advanced=True, group="Identity",
        help="The MCP server that handled the call.",
    ),
    Filter(
        key="tool", label="Tool", column="mcp_tool_name", source="tools",
        advanced=True, group="Identity",
    ),
    Filter(
        key="method", label="Method", column="mcp_method", source="methods",
        advanced=True, group="Identity",
        help="tools/call, tools/list, prompts/get and the rest of the protocol.",
    ),
    Filter(
        key="transport", label="Transport", column="transport", group="Identity",
        advanced=True,
        choices=(("", "Any transport"), ("stdio", "stdio"),
                 ("streamable-http", "streamable-http")),
        help=(
            "How the client reached the server. stdio servers are spawned per "
            "client and are the common deployment; HTTP servers are long-lived "
            "and shared."
        ),
    ),
    Filter(
        key="status", label="Status", group="Outcome", pinned=True,
        choices=(("", "Any status"), ("ok", "Succeeded"), ("error", "Failed")),
        # Constants, selected by the value. `cancelled` and `pending_input` are
        # excluded from "Failed" for the same reason the error list excludes
        # them: an awaiting-input round is not a failure (D20), and a client
        # that gave up is not a server fault.
        predicates=(
            ("error", f"failure_category NOT IN ({_NOT_A_FAILURE_SQL})"),
            ("ok", "failure_category IN ('', 'ok')"),
        ),
        help="Coarser than category: did the call succeed at all.",
    ),
    Filter(
        key="failure_category", label="Category", column="failure_category",
        source="categories", advanced=True, group="Outcome",
        help="How it failed, from the MCP-aware classifier.",
    ),
    Filter(
        key="min_duration_ms", label="Slower than", kind=NUMBER, column="duration_ms",
        op=">=", ch_type="Float64", group="Duration", placeholder="ms",
        help="Whole-trace duration, in milliseconds.",
    ),
    Filter(
        key="max_duration_ms", label="Faster than", kind=NUMBER, column="duration_ms",
        op="<=", ch_type="Float64", group="Duration", placeholder="ms",
    ),
)

# Capability tables. `{name}` becomes the column naming the item, which differs
# per kind -- one query path for tools, prompts, resources and protocol methods.
CAPABILITY_FILTERS: tuple[Filter, ...] = (
    Filter(
        key="q", label="Search", kind=SEARCH, columns=("{name}",), pinned=True,
        placeholder="Search by name…", group="Search",
    ),
    Filter(
        key="server", label="Server", column="service_name", source="servers",
        group="Identity",
    ),
    Filter(
        key="errors_only", label="Has errors", kind=TOGGLE, stage=HAVING,
        column="errors > 0", pinned=True, group="Outcome",
        help="Only entries that have failed at least once in this window.",
    ),
    Filter(
        key="min_calls", label="At least", kind=NUMBER, stage=HAVING, column="calls",
        op=">=", ch_type="UInt32", maximum=1_000_000, group="Volume", placeholder="calls",
        help="Hide the long tail of things called once.",
    ),
    Filter(
        key="sort", label="Sort by", stage=SORT, group="Sort",
        choices=(("calls", "Most called"), ("errors", "Most errors"),
                 ("p95", "Slowest p95"), ("name", "Name A-Z"),
                 ("last_seen", "Recently used")),
    ),
)

#: Sort key -> ORDER BY. Looked up here, never taken from the query string:
#: ClickHouse cannot parameterise an identifier, so an unchecked `?sort=` would
#: be raw SQL from a URL.
SORTS: Mapping[str, str] = {
    "calls": "calls DESC",
    "errors": "errors DESC, calls DESC",
    "name": "item ASC",
    "last_seen": "last_seen DESC",
    "p95": "p95_sort DESC",
}

VIEWS: Mapping[str, tuple[Filter, ...]] = {
    "traces": TRACE_FILTERS,
    # Same filters as traces. `status` is dropped: the list is already
    # failures-only, so "Succeeded" would be a control that always returns
    # nothing -- worse than an absent one.
    "errors": tuple(f for f in TRACE_FILTERS if f.key != "status"),
    "capabilities": CAPABILITY_FILTERS,
}

#: Operators the advanced rows offer. Small on purpose: each must be expressible
#: as a bound parameter.
OPERATORS: tuple[tuple[str, str], ...] = (
    ("is", "is"), ("is_not", "is not"), ("contains", "contains"),
)

MAX_CONDITIONS = 10


#: Prefix marking a condition's `field` as a `span_attributes` map lookup
#: rather than a lookup against one of this view's enumerated `Filter`
#: specs -- e.g. `where=attr.mcpobs.custom.request_id:is:abc-123`. Storage
#: places no restriction on attribute keys (normalizer/normalize.py copies
#: every incoming attribute through unfiltered), so this stays a literal map
#: lookup rather than assuming any one SDK's namespacing convention.
ATTR_FIELD_PREFIX = "attr."

#: Views whose `attr.` conditions make sense. Both share `TRACE_FILTERS`'s
#: per-trace aggregation, where "does this trace have a span with this
#: attribute" maps onto an EXISTS check against `trace_id`. `capabilities`
#: aggregates by tool/prompt/resource name across the whole window, where
#: one span's attribute doesn't correspond to one aggregate row the same way
#: -- an `attr.` condition there is dropped, same as any other invalid row.
_ATTR_VIEWS = ("traces", "errors")


@dataclass
class Condition:
    """One advanced row: field, operator, value."""

    field: str
    op: str
    value: str

    def is_attr(self) -> bool:
        return self.field.startswith(ATTR_FIELD_PREFIX)

    def attr_key(self) -> str:
        return self.field[len(ATTR_FIELD_PREFIX) :].strip()

    def spec(self, view: str) -> Filter | None:
        return next(
            (f for f in VIEWS.get(view, ()) if f.key == self.field and f.advanced), None
        )

    def valid(self, view: str) -> bool:
        if self.op not in dict(OPERATORS) or self.value.strip() == "":
            return False
        if self.is_attr():
            return view in _ATTR_VIEWS and self.attr_key() != ""
        return self.spec(view) is not None


@dataclass
class Filters:
    """Parsed filter values for one view, plus its advanced conditions."""

    view: str
    values: dict[str, Any] = field(default_factory=dict)
    conditions: list[Condition] = field(default_factory=list)

    def specs(self) -> tuple[Filter, ...]:
        return VIEWS.get(self.view, ())

    # ------------------------------------------------------------------- sql
    def clauses(self, stage: str, params: dict[str, Any], name_column: str = "") -> list[str]:
        """SQL fragments for one stage, binding every value into `params`."""
        out = []
        for spec in self.specs():
            if spec.stage != stage:
                continue
            clause = spec.clause(self.values.get(spec.key), params, name_column)
            if clause:
                out.append(clause)
        if stage == WHERE:
            out.extend(self._condition_clauses(params, name_column))
        return out

    def _condition_clauses(self, params: dict[str, Any], name_column: str) -> list[str]:
        out = []
        for i, cond in enumerate(self.conditions):
            # Invalid rows are SKIPPED, not rejected: a half-typed row is the
            # normal state of one being filled in, and 400-ing on it would make
            # the panel unusable while you type.
            if not cond.valid(self.view):
                continue
            if cond.is_attr():
                out.append(self._attr_clause(cond, i, params))
                continue
            spec = cond.spec(self.view)
            assert spec is not None
            column = spec.column.replace("{name}", name_column)
            key = f"f_c{i}"
            params[key] = cond.value.strip()
            if cond.op == "contains":
                out.append(f"positionCaseInsensitive({column}, {{{key}:String}}) > 0")
            elif cond.op == "is_not":
                out.append(f"{column} != {{{key}:String}}")
            else:
                out.append(f"{column} = {{{key}:String}}")
        return out

    @staticmethod
    def _attr_clause(cond: Condition, i: int, params: dict[str, Any]) -> str:
        """`attr.<key>` conditions check whether ANY span in the trace
        carries the given attribute -- NOT a column on the aggregated trace
        row this clause otherwise runs against. `span_attributes` only
        exists at the per-span level, inside the subquery `traces()`/
        `errors()` group by trace_id; filtering there directly would corrupt
        span_count/duration for the traces it keeps (see this file's module
        docstring on WHERE vs per-span filtering). An EXISTS-style check
        against `trace_id`, which the outer query does have, avoids that.

        Reuses the SAME tenant_id/project_id/since parameters `_scope()`
        already bound into `params` for the outer query (query/repository.py)
        -- required so this can never leak cross-tenant matches or ignore the
        time window; verified against the exact scoping clause used
        elsewhere in repository.py (`WHERE tenant_id = {tenant:String} AND
        project_id = {project:String} AND timestamp >= {since:DateTime}`).
        """
        key_param, val_param = f"f_c{i}_key", f"f_c{i}_val"
        params[key_param] = cond.attr_key()
        params[val_param] = cond.value.strip()
        lookup = f"span_attributes[{{{key_param}:String}}]"
        # mapContains, not a bare != -- ClickHouse's Map subscript returns ''
        # for a key that was never set (String's type default), not NULL.
        # Without the existence check, "is_not X" silently matched every span
        # that never set the attribute at all (''  != X is true), not just
        # the ones that set it to something else.
        has_key = f"mapContains(span_attributes, {{{key_param}:String}})"
        if cond.op == "contains":
            comparison = f"positionCaseInsensitive({lookup}, {{{val_param}:String}}) > 0"
        elif cond.op == "is_not":
            comparison = f"{has_key} AND {lookup} != {{{val_param}:String}}"
        else:
            comparison = f"{lookup} = {{{val_param}:String}}"
        return (
            "trace_id IN (SELECT trace_id FROM spans_raw WHERE "
            "tenant_id = {tenant:String} AND project_id = {project:String} "
            f"AND timestamp >= {{since:DateTime}} AND {comparison})"
        )

    def order_by(self) -> str:
        return SORTS.get(str(self.values.get("sort") or "calls"), SORTS["calls"])


# ===========================================================================
# Parsing and cataloguing
# ===========================================================================
def parse(view: str, query: Mapping[str, str], where: Sequence[str] = ()) -> Filters:
    """Query string -> Filters. Unknown parameters are ignored, not rejected."""
    values: dict[str, Any] = {}
    for spec in VIEWS.get(view, ()):
        raw = query.get(spec.key)
        if raw is None or raw == "":
            continue
        try:
            parsed = spec.parse(raw)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if parsed not in (None, ""):
            values[spec.key] = parsed

    conditions = []
    for raw in list(where)[:MAX_CONDITIONS]:
        # `field:op:value`, colon separated. Values are tool and server names,
        # which never contain one -- and a JSON blob in a query string would
        # make a shared link unreadable, which defeats putting filters in the
        # URL at all.
        f, _, rest = raw.partition(":")
        op, _, value = rest.partition(":")
        cond = Condition(field=f, op=op or "is", value=value)
        if cond.valid(view):
            conditions.append(cond)

    return Filters(view=view, values=values, conditions=conditions)


def catalog(view: str, options: Any) -> dict[str, Any]:
    """Everything the browser needs to render this view's filter panel.

    Including the option VALUES, resolved from the data in this window. The
    panel makes no second request and holds no list of its own.
    """
    specs = VIEWS.get(view, ())
    groups: list[dict[str, Any]] = []
    for spec in specs:
        entry = spec.catalog(options)
        group = next((g for g in groups if g["name"] == spec.group), None)
        if group is None:
            group = {"name": spec.group, "filters": []}
            groups.append(group)
        group["filters"].append(entry)

    return {
        "view": view,
        "groups": groups,
        "advanced": {
            "fields": [{"value": f.key, "label": f.label} for f in specs if f.advanced],
            "operators": [{"value": v, "label": lbl} for v, lbl in OPERATORS],
            "max": MAX_CONDITIONS,
        },
    }


def openapi_parameters(view: str) -> list[dict[str, Any]]:
    """OpenAPI parameter list generated from the same config.

    The endpoints read their filters off the raw query string, which would
    otherwise leave them undocumented. Generating the docs from the config keeps
    "one entry adds a filter" true without the API becoming opaque.
    """
    types = {NUMBER: "number", TOGGLE: "boolean"}
    out = [
        {
            "name": spec.key,
            "in": "query",
            "required": False,
            "description": spec.help or spec.label,
            "schema": {"type": types.get(spec.kind, "string")},
        }
        for spec in VIEWS.get(view, ())
    ]
    if any(spec.advanced for spec in VIEWS.get(view, ())):
        out.append({
            "name": "where",
            "in": "query",
            "required": False,
            "description": (
                "Advanced condition as field:op:value. Repeatable, AND-ed. "
                "field may be one of this view's advanced filter keys, or "
                "attr.<key> to match a custom span attribute (traces/errors "
                "views only), e.g. attr.mcpobs.custom.request_id:is:abc-123."
            ),
            "schema": {"type": "array", "items": {"type": "string"}},
        })
    return out
