# MCP Observability

Observability built for the Model Context Protocol, not adapted to it.

A generic tracing tool shows you that a request took 800ms and returned 200.
This shows you that `place_order` failed because the payment provider returned
503, that the call spent 391ms of its 538ms waiting on shipping, and that the
tool has failed 4 times in the last hour — because it understands MCP as a
protocol rather than as HTTP traffic.

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Get started](get-started/quickstart.md)**

    Two lines in your server, and traces within a minute.

-   :material-power-plug: **[Integrate](integrate/python-sdk.md)**

    The Python SDK, and how to see your Postgres, Redis and HTTP calls.

-   :material-lightbulb: **[Concepts](concepts/failures.md)**

    Why a 401 is not a failure, and what "awaiting input" means.

-   :material-book-open-variant: **[Reference](reference/sdk.md)**

    Every public function, its arguments and its behaviour.

</div>

## What makes it MCP-native

**Failures are classified, not just counted.** `tool_error`, `server_exception`,
`unknown_tool`, `invalid_arguments`, `protocol_error` — each is a different
problem with a different owner. A single "error rate" collapses all of them into
one number that tells you nothing about what to do next. See
[Failure taxonomy](concepts/failures.md).

**Protocol reality is respected.** A multi-round-trip tool that pauses to ask
the user a question is *not* an error, and counting it as one is the fastest way
to corrupt an error rate. A `401` is not a server failure — the MCP
authorization flow *opens* with an unauthenticated request answered by a 401.
Both are categorised on their own terms.

**Downstream work is attributed.** Your tool's database queries, cache lookups
and outbound API calls appear as child spans with their own timing, so a slow
tool says *where* the time went instead of looking like unexplained server time.

## What it does not do

**It does not capture your tool payloads by default.** Error classification works
by reading SDK-generated boilerplate in your process and reducing it to a single
enum before anything leaves. Payload capture is a separate, explicit opt-in —
see [Payloads and privacy](integrate/payloads.md).

**It does not patch your database drivers behind your back.** Downstream
instrumentation is something you turn on, in one call, and it tells you what it
touched. See [Databases, caches and APIs](integrate/downstream.md).
