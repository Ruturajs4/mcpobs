"""T3 -- the observed-attribute report.

Runs every demo scenario over BOTH transports, captures the spans the MCP SDK
actually emitted, and writes docs/observed_attributes.md.

This is the never-cut task: everything downstream codes against this report,
not against any document's expectations (Day-1 doc D10). Needs no Docker --
the demo server exports spans to an NDJSON file from its own subprocess.

    python scripts/dump_observed_attrs.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from demo_server.scenarios import http_session, run_scenarios, stdio_session  # noqa: E402

OUT_PATH = REPO_ROOT / "docs" / "observed_attributes.md"
CAPTURE_DIR = REPO_ROOT / ".captures"

TRACKED_PACKAGES = [
    "mcp",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-proto",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-httpx",
    "httpx",
    "confluent-kafka",
    "clickhouse-connect",
]

# What Day-1 doc 4.2 expected, so we can report what is genuinely absent.
DOC_EXPECTED = [
    "mcp.method.name",
    "gen_ai.tool.name",
    "gen_ai.prompt.name",
    "gen_ai.operation.name",
    "mcp.resource.uri",
    "error.type",
    "rpc.response.status_code",
    "jsonrpc.request.id",
    "mcp.protocol.version",
    "mcp.session.id",
    "network.transport",
    "network.protocol.name",
    "network.protocol.version",
    "jsonrpc.protocol.version",
    "client.address",
    "client.port",
    "server.address",
    "server.port",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
]


def read_spans(path: Path) -> list[dict]:
    if not path.exists():
        return []
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                spans.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return spans


async def capture(transport: str, span_file: Path) -> list[dict]:
    span_file.unlink(missing_ok=True)
    session = stdio_session if transport == "stdio" else http_session
    async with session(span_file=span_file) as client:
        await run_scenarios(client)
    await asyncio.sleep(1.0)  # let the subprocess flush and exit
    return read_spans(span_file)


def mcp_spans(spans: list[dict]) -> list[dict]:
    return [s for s in spans if "mcp.method.name" in (s.get("attributes") or {})]


def attr_keys(spans: list[dict]) -> set[str]:
    keys: set[str] = set()
    for span in spans:
        keys |= set((span.get("attributes") or {}).keys())
    return keys


def fmt_table(rows: list[list[str]], headers: list[str]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def build_report(by_transport: dict[str, list[dict]]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_spans = [s for spans in by_transport.values() for s in spans]
    all_mcp = mcp_spans(all_spans)

    lines: list[str] = [
        "# Observed attribute report (T3)",
        "",
        "> **GENERATED FILE — do not hand-edit.** Regenerate with "
        "`python scripts/dump_observed_attrs.py`.",
        "",
        "This report is the source of truth for what the MCP Python SDK actually emits. "
        "Where it disagrees with the Day-1 engineering document, **this report wins** "
        "(Day-1 doc D10).",
        "",
        f"Captured: {now}",
        "",
        "## Resolved versions",
        "",
    ]

    version_rows = []
    for pkg in TRACKED_PACKAGES:
        try:
            version_rows.append([f"`{pkg}`", f"`{version(pkg)}`"])
        except Exception:
            version_rows.append([f"`{pkg}`", "_not installed_"])
    lines += [fmt_table(version_rows, ["Package", "Version"]), ""]

    # ---- span inventory -------------------------------------------------
    lines += ["## Span inventory", ""]
    inv_rows = []
    for transport, spans in by_transport.items():
        for span in mcp_spans(spans):
            attrs = span.get("attributes") or {}
            inv_rows.append([
                f"`{transport}`",
                f"`{span.get('name')}`",
                f"`{span.get('kind')}`",
                f"`{(span.get('status') or {}).get('status_code', '')}`",
                f"`{attrs.get('error.type', '')}`" if attrs.get("error.type") else "—",
                str(len(span.get("events") or [])),
            ])
    lines += [
        fmt_table(inv_rows, ["Transport", "Span name", "Kind", "Status", "error.type", "Events"]),
        "",
    ]

    # ---- attributes actually emitted ------------------------------------
    lines += ["## Attributes emitted on MCP spans", ""]
    observed = sorted(attr_keys(all_mcp))
    obs_rows = []
    for key in observed:
        seen_on = [
            span.get("name")
            for span in all_mcp
            if key in (span.get("attributes") or {})
        ]
        example = next(
            (span["attributes"][key] for span in all_mcp if key in (span.get("attributes") or {})),
            "",
        )
        obs_rows.append([
            f"`{key}`",
            f"`{type(example).__name__}`",
            f"{len(seen_on)}/{len(all_mcp)}",
            f"`{str(example)[:44]}`",
        ])
    lines += [fmt_table(obs_rows, ["Attribute", "Python type", "Spans", "Example"]), ""]

    # ---- expected but absent --------------------------------------------
    absent = [k for k in DOC_EXPECTED if k not in observed]
    lines += [
        "## Expected by the Day-1 doc but NOT emitted",
        "",
        "These appear in Day-1 doc §4.2. The SDK does not emit them; the corresponding "
        "columns will be NULL.",
        "",
    ]
    lines += [f"- `{k}`" for k in absent] or ["- _(none)_"]
    lines += [""]

    # ---- failure taxonomy reachability ----------------------------------
    lines += ["## Failure taxonomy reachability", ""]
    error_types = sorted({
        (s.get("attributes") or {}).get("error.type")
        for s in all_mcp
        if (s.get("attributes") or {}).get("error.type")
    })
    failing = [s for s in all_mcp if (s.get("status") or {}).get("status_code") == "ERROR"]
    lines += [
        f"Distinct `error.type` values observed across all failure scenarios: "
        + (", ".join(f"`{e}`" for e in error_types) or "_none_"),
        "",
        f"Failing spans: {len(failing)} · spans carrying `rpc.response.status_code`: "
        f"{sum(1 for s in all_mcp if 'rpc.response.status_code' in (s.get('attributes') or {}))} · "
        f"spans carrying exception events: {sum(1 for s in all_mcp if s.get('events'))}",
        "",
    ]
    if len(error_types) <= 1 and len(failing) > 1:
        lines += [
            "> ### FINDING: the failure taxonomy is not reachable from span attributes",
            ">",
            "> Four deliberately different failure modes were exercised — a tool returning",
            "> `isError=True`, a handler raising `RuntimeError`, a call to an unknown tool, and a",
            "> schema-violating argument. **All four produce an identical span**: status `ERROR`,",
            "> `error.type=\"tool_error\"`, no `rpc.response.status_code`, no exception event.",
            ">",
            "> Cause: `MCPServer`'s tool handler catches everything and converts it to a",
            "> `CallToolResult(isError=True)` *before* `OpenTelemetryMiddleware` observes the",
            "> result, so the middleware's `except Exception` and `except MCPError` branches are",
            "> unreachable for anything routed through `tools/call`.",
            ">",
            "> **Consequences.** Day-1 doc §9.5 lists five categories; only `ok` and `tool_error`",
            "> are reachable. Assertion A3 cannot pass as originally written. V2 §25's launch",
            "> checklist item *\"MCP isError and thrown exception are distinguishable\"* is **not",
            "> achievable from span attributes** with the stock SDK.",
            ">",
            "> **The product angle.** The differentiator V2 §6.3 sells is an MCP failure taxonomy.",
            "> If every failure looks the same on the span, that taxonomy needs a source beyond",
            "> attributes — result-content inspection (a payload feature, opt-in per V2 §15) or an",
            "> upstream SDK change. This is the single most important thing Day 1 found and it",
            "> belongs in the Day-2 agenda, not in a backlog.",
            "",
        ]

    # ---- transport diff --------------------------------------------------
    lines += ["## Transport comparison", ""]
    keysets = {t: attr_keys(mcp_spans(s)) for t, s in by_transport.items()}
    transports = list(keysets)
    if len(transports) == 2:
        a, b = transports
        only_a = sorted(keysets[a] - keysets[b])
        only_b = sorted(keysets[b] - keysets[a])
        lines += [
            f"- Attributes on `{a}` only: " + (", ".join(f"`{k}`" for k in only_a) or "_none_"),
            f"- Attributes on `{b}` only: " + (", ".join(f"`{k}`" for k in only_b) or "_none_"),
            f"- Shared: {len(keysets[a] & keysets[b])} attributes",
            "",
        ]
        if not only_a and not only_b:
            lines += [
                "**The two transports emit identical attribute sets.** Transport is therefore "
                "not observable from span attributes alone on Day 1 — `network.transport` is "
                "not emitted, so the normalizer cannot distinguish stdio from streamable HTTP.",
                "",
            ]

    # ---- child spans -----------------------------------------------------
    lines += ["## Downstream child spans (A4)", ""]
    child_rows = []
    for transport, spans in by_transport.items():
        mcp_ids = {
            (s.get("context") or {}).get("span_id") for s in mcp_spans(spans)
        }
        for span in spans:
            if span.get("parent_id") in mcp_ids and "mcp.method.name" not in (span.get("attributes") or {}):
                attrs = span.get("attributes") or {}
                child_rows.append([
                    f"`{transport}`",
                    f"`{span.get('name')}`",
                    f"`{attrs.get('http.request.method', attrs.get('http.method', ''))}`",
                    f"`{attrs.get('http.response.status_code', attrs.get('http.status_code', ''))}`",
                ])
    lines += [
        fmt_table(child_rows, ["Transport", "Child span", "Method", "Status"])
        if child_rows
        else "_No child spans captured — A4 would fail._",
        "",
    ]

    return "\n".join(lines) + "\n"


async def main() -> None:
    CAPTURE_DIR.mkdir(exist_ok=True)
    OUT_PATH.parent.mkdir(exist_ok=True)

    by_transport: dict[str, list[dict]] = {}
    for transport in ("stdio", "http"):
        print(f"capturing {transport} ...")
        by_transport[transport] = await capture(transport, CAPTURE_DIR / f"{transport}.ndjson")
        print(f"  {len(by_transport[transport])} spans "
              f"({len(mcp_spans(by_transport[transport]))} MCP)")

    OUT_PATH.write_text(build_report(by_transport), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
