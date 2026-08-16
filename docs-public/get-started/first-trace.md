# Your first trace

A trace is one tool call and everything it caused. Click any row in **Traces**
and it opens in a drawer beside the list — debugging is comparison, so the list
stays where it was.

## Reading the waterfall

Here is a real `place_order` call:

```
POST /mcp                    541.0ms  ROOT
  tools/list                   2.6ms
  tools/call place_order     538.4ms
    SELECT   mysql SELECT       <1ms
    UPDATE   mysql UPDATE       <1ms
    POST                       25.3ms   inventory API
    POST                       59.9ms   payments API
    POST                      391.8ms   shipping API
    INSERT   postgresql        2.11ms
    DEL      redis DEL         14.8ms
```

Three things are worth noticing.

**The root is the HTTP request, not the tool call.** On the streamable-HTTP
transport your server receives a POST, and the MCP call happens inside it. Both
are real, and the nesting is what tells you transport overhead was 2.6ms.

**One child dominates.** 391.8ms of the 538.4ms is a single call to the shipping
API. Without child spans that tool simply looks slow; with them, the answer is
already on screen.

**`tools/list` appears in the same trace.** Clients call it on connect. It is
not called before every tool call, and if it is slow that is a real symptom your
users feel at connection time.

## Self-time

Each span shows **self-time** — its total minus the sum of its children. That is
the difference between *this tool is slow* and *this tool is waiting on
something slow*, which duration alone cannot distinguish.

## When a span is red

Open it. A failing span carries its category, and where the helper captured it,
the error text that produced the classification. A child span can be red while
the tool call itself is green: a partner API returned 503 and your tool handled
it. That is not a contradiction — it is the case a tool-level dashboard hides.

## Next

- [Failure taxonomy](../concepts/failures.md) — what each category means.
- [Filtering and search](../console/filtering.md) — narrowing a busy list.
