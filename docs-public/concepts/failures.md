# Failure taxonomy

A single error rate tells you something is wrong. It does not tell you whose
problem it is. These categories do.

## The five failures

| Category | Means | Usually your |
| --- | --- | --- |
| `tool_error` | The tool ran and returned an error result | Business logic |
| `server_exception` | The tool raised an unhandled exception | Bug or a dependency |
| `unknown_tool` | The client called a tool that does not exist | Client or a version mismatch |
| `invalid_arguments` | Arguments failed validation before the tool ran | Client, or a schema change |
| `protocol_error` | Malformed request; never reached a tool | Client or transport |

`unclassified` exists for spans that arrived without enough information to
categorise, and is counted as a failure so nothing hides in it.

## Three things that are *not* failures

This is the part that most affects your numbers.

!!! success "`pending_input` — a multi-round-trip call awaiting an answer"

    A tool that pauses to ask the user a question emits an interim round. It has
    not failed; it is waiting. Counting these is the single most likely way to
    corrupt an error rate, because they are indistinguishable from errors in a
    generic tracing tool.

!!! success "`cancelled` — the client gave up"

    Not a success, and not a server fault. Counted separately from both. It is
    also excluded from latency percentiles: a cancelled call's duration is
    truncated at the moment the client stopped waiting, so including it makes a
    tool that was cancelled *because* it was slow improve your p95.

!!! success "`unauthorized` / `forbidden` — transport-level 401 and 403"

    The MCP authorization flow **opens** with an unauthenticated request
    answered by a 401, and `403 insufficient_scope` drives the routine step-up
    flow. Counting either as a server failure would make every correctly
    behaving client look broken.

## One definition, everywhere

The error rate on the Overview, the `status=error` filter, and the Errors list
all use the **same** definition of failure. This is worth stating because it is
easy to get wrong: an Errors page that lists traces the headline error rate says
are not errors makes both numbers untrustworthy.

## Two data qualities, never mixed

Servers running the `mcpobs` helper report precise categories. Servers exporting
plain OTel spans report the coarse `tool_error`. Every row records which source
classified it, and the console shows you the share that is precise — so you
always know how much of the breakdown is real.

See [Failure categories](../reference/failure-categories.md) for the complete
list with the exact conditions that produce each.
