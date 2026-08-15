"""U5.2 -- the MRTR stitching experiment.

THE QUESTION (Day-1 doc §14, Day-2 doc §9.2)
    Under Multi Round-Trip Requests, a server needing more information returns
    `resultType: "input_required"` and the client RETRIES the original request
    carrying `inputResponses`. So one logical tool call becomes two or more
    wire requests.

    Do we stitch those round-trips into a single logical call, or report them
    separately? The answer defines what "tool latency" MEANS in every chart the
    product will ever draw, so it is decided by experiment rather than argument.

WHAT WE NEED TO KNOW
    1. Do the round-trips share a trace_id? (Can we correlate them at all?)
    2. How many spans does one logical call produce?
    3. Is user-perceived latency -- including client think-time -- reconstructible
       from what the server observes?

Run:  python scripts/mrtr_experiment.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.client import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcpobs import instrument  # noqa: E402

CLIENT_THINK_TIME = 0.4
"""Simulated human/agent delay between being asked and answering."""


class Confirmation(BaseModel):
    approved: bool


def ask_confirmation() -> Elicit[Confirmation]:
    """Resolver: the framework turns this into an InputRequiredResult.

    NOTE: `ctx.elicit()` does NOT take this path -- it issues a legacy
    server-initiated `elicitation/create` over a back-channel, which does not
    exist on a stateless 2026-07-28 transport (NoBackChannelError). The
    Resolve/Elicit annotation is the MRTR-native mechanism.
    """
    return Elicit("Deploy to production?", Confirmation)


# Module level, not nested: the SDK evaluates tool annotations by name, and
# `from __future__ import annotations` makes locals unresolvable.
async def deploy(
    confirmation: Annotated[Confirmation, Resolve(ask_confirmation)],
    service: str = "api",
) -> str:
    """A tool that must ask before acting -- the canonical MRTR case."""
    return f"deploy {service}: {'done' if confirmation.approved else 'declined'}"


def build_server() -> MCPServer:
    server = MCPServer("mrtr-experiment", version="0.0.1")
    server.tool()(deploy)
    return instrument(server)


async def on_elicit(*args: Any, **kwargs: Any) -> Any:
    """Client-side handler: pause, then answer -- the second round trip."""
    await asyncio.sleep(CLIENT_THINK_TIME)
    return {"action": "accept", "content": {"approved": True}}


def report(spans: list[Any]) -> None:
    print(f"\nSPANS PRODUCED: {len(spans)}")
    traces: dict[str, list[Any]] = {}
    for span in spans:
        traces.setdefault(f"{span.context.trace_id:032x}", []).append(span)

    print(f"DISTINCT TRACES: {len(traces)}\n")
    for trace_id, group in traces.items():
        group.sort(key=lambda s: s.start_time)
        span_ms = sum((s.end_time - s.start_time) for s in group) / 1e6
        wall_ms = (max(s.end_time for s in group) - min(s.start_time for s in group)) / 1e6
        print(f"  trace {trace_id[:16]}...  {len(group)} span(s)")
        for span in group:
            attrs = span.attributes or {}
            marks = []
            if "mcpobs.result.type" in attrs:
                marks.append(f"resultType={attrs['mcpobs.result.type']}")
            if "mcpobs.failure.kind" in attrs:
                marks.append(f"kind={attrs['mcpobs.failure.kind']}")
            if "mcpobs.mrtr.state.out" in attrs:
                marks.append(f"state.out={attrs['mcpobs.mrtr.state.out']}")
            if "mcpobs.mrtr.state.in" in attrs:
                marks.append(f"state.in={attrs['mcpobs.mrtr.state.in']}")
            print(
                f"    {span.name:<26} {(span.end_time - span.start_time) / 1e6:8.2f}ms"
                f"  {' '.join(marks)}"
            )
        print(f"    sum(span durations) = {span_ms:.2f}ms")
        print(f"    wall clock          = {wall_ms:.2f}ms")

    tool_spans = [s for s in spans if s.name.startswith("tools/call")]

    # Can we chain the rounds? Round N emits state.out; round N+1 receives the
    # same value as state.in. That pairing is the only link, since the rounds
    # are separate traces.
    emitted = {
        (s.attributes or {}).get("mcpobs.mrtr.state.out"): s
        for s in tool_spans
        if (s.attributes or {}).get("mcpobs.mrtr.state.out")
    }
    chained = [
        (emitted[key], s)
        for s in tool_spans
        if (key := (s.attributes or {}).get("mcpobs.mrtr.state.in")) in emitted
    ]

    print("\n--- FINDINGS ---")
    print(f"1. tools/call spans for ONE logical call : {len(tool_spans)}")
    print(f"2. round-trips share a trace_id          : {len(traces) == 1}")
    print(f"2b. chainable via mcpobs.mrtr.state       : {len(chained)} link(s)")
    for first, second in chained:
        span_ms = (first.end_time - first.start_time + second.end_time - second.start_time) / 1e6
        logical_ms = (second.end_time - first.start_time) / 1e6
        print(f"    {first.name} -> {second.name}")
        print(f"      server time across rounds  = {span_ms:.2f}ms")
        print(f"      LOGICAL call wall clock    = {logical_ms:.2f}ms  <- includes client think-time")
    if tool_spans:
        observed = sum((s.end_time - s.start_time) for s in tool_spans) / 1e6
        print(f"3. server-observed tool time             : {observed:.2f}ms")
        print(f"   client think-time (not observed)      : {CLIENT_THINK_TIME * 1000:.0f}ms")
        print(
            f"   => server-observed latency understates user-perceived by "
            f"~{CLIENT_THINK_TIME * 1000 / max(observed, 0.01):.0f}x"
        )


async def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    server = build_server()
    async with Client(server, elicitation_callback=on_elicit) as client:
        result = await client.call_tool("deploy", {"service": "checkout"})
        text = next(
            (getattr(b, "text", None) for b in (getattr(result, "content", None) or [])),
            None,
        )
        print(f"tool returned: {text!r}")

    report(list(exporter.get_finished_spans()))


if __name__ == "__main__":
    asyncio.run(main())
