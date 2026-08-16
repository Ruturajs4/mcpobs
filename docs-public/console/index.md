# The console

Six views, in the order you use them.

| View | Answers |
| --- | --- |
| **Overview** | Is the fleet healthy right now, and how is it failing? |
| **Servers** | Which server, which version, which environment? |
| **Tools / Prompts / Resources** | Which capability is slow or failing? |
| **Protocol** | Is `tools/list` slow? It runs on every client connect. |
| **Traces** | What happened on this specific call? |
| **Errors** | Only the failures, newest first. |

## Reading the Overview

- **Tool calls** — volume in the selected window.
- **Error rate** — failures over calls, using the
  [single failure definition](../concepts/failures.md#one-definition-everywhere).
- **p95 latency** — over eligible spans, with a caveat when the host clock
  cannot support it.
- **Failure breakdown** — the categories, sorted. Click one to see those traces.

Banners appear above the cards when something qualifies the numbers: an
unreliable clock, or a share of failures that are only coarsely classified.

## Live vs paused

Overview and Servers refresh every 30 seconds. Traces and Errors do **not** —
investigation surfaces should not move while you read them. The sidebar always
shows how old the data is, and says why refreshing is paused when it is.

## The trace drawer

Clicking a trace opens it beside the list rather than replacing it. Debugging is
comparison — you bounce between traces, and navigating away loses your place.
`Esc` closes it. The URL carries the trace, so it is a link you can send.
