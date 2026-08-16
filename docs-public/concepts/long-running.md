# Long-running tools

A tool that takes ten seconds is invisible for those ten seconds in most tracing
systems, because a span is only exported when it ends. MCP has protocol
machinery for this, and it is observed.

## Progress

Each `ctx.report_progress()` call becomes a **child span of the running tool
call**, carrying value, total, percentage and message. They are queryable while
the parent is still running — so a 10-second export shows its progress at second
three, not at second eleven.

```python
@mcp.tool()
async def slow_export(ctx: Context, rows: int = 100) -> str:
    for i in range(rows):
        await ctx.report_progress(i + 1, rows, f"exported {i + 1}/{rows}")
    return "done"
```

!!! note "Capped at 200 per operation"

    A runaway loop reporting progress thousands of times would otherwise flood
    your telemetry. The cap announces itself on the span rather than silently
    dropping.

## Cancellation

When a client gives up, the call is recorded as `cancelled` — not `ok`, not an
error, and **not** a latency sample. See
[Failure taxonomy](failures.md#three-things-that-are-not-failures).

## Subscriptions

Events delivered on a subscription stream become individual spans, so throughput
and delivery gaps are visible in real time rather than only when the stream
closes.

```python
from mcpobs import ObservedSubscriptionBus

bus = ObservedSubscriptionBus(InMemorySubscriptionBus())
```

!!! warning "A quiet subscription is currently indistinguishable from a healthy one"

    A stream that has *stopped* carrying events shows as an absence of recent
    event spans. A subscription that has never carried one — opened and quiet —
    produces nothing until it ends. Answering "is this client still listening?"
    directly would need a heartbeat, which is telemetry emitted for nothing
    happening; we would rather discuss that trade with you than assume it.

## Multi-round-trip requests

`roots/list`, `sampling/createMessage` and `elicitation/create` pause a call to
ask the client something. The interim round is recorded as `pending_input` with
its MRTR state, and is excluded from both your error rate and your latency
percentiles.
