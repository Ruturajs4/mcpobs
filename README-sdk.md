# mcpobs

Observability for Model Context Protocol servers, built for the protocol rather
than adapted to it.

A generic tracing tool tells you a request took 800ms and returned 200. This
tells you which tool failed, *why* it failed, and where the time went — because
it understands MCP as a protocol.

```bash
pip install mcpobs
```

```python
from mcpobs import instrument

instrument(mcp)          # that is the whole integration
```

## What you get

**Failures are classified, not counted.** `tool_error`, `server_exception`,
`unknown_tool`, `invalid_arguments`, `protocol_error` — each is a different
problem with a different owner. One "error rate" collapses them into a number
that cannot tell you what to do next.

**Protocol reality is respected.** A multi-round-trip tool pausing to ask the
user a question is *not* an error, and counting it as one is the fastest way to
corrupt an error rate. A `401` is not a server failure — the MCP authorization
flow *opens* with an unauthenticated request answered by one. Both are
categorised on their own terms.

**Progress and cancellation are visible while they happen.** Each
`report_progress()` becomes a child span, queryable *during* a long call rather
than when it ends. A cancelled call is recorded as cancelled — not as a fast
success, which is what a truncated duration would otherwise look like.

## Your database, cache and API calls

One call, and it reports what it touched:

```python
from mcpobs import instrument, instrument_downstream

instrument(mcp)
report = instrument_downstream()
# {'httpx': 'instrumented', 'psycopg': 'instrumented', 'redis': 'instrumented'}
```

It discovers whatever you have installed through OpenTelemetry's own entry-point
group, so `pip install opentelemetry-instrumentation-redis` is the entire
integration. **This package depends on none of them** — you install the
instrumentors you actually use.

It never raises: a package with a version conflict is reported and skipped, not
allowed to stop your server booting.

## What does not leave your process

Error classification reads SDK-generated boilerplate and reduces it to a single
enum before anything is exported. Tool arguments and results are **not**
captured by default.

On failing calls only, a truncated error message is recorded so you can see
*why* something failed. `instrument(mcp, capture_error_detail=False)` turns that
off.

## Sending less than everything

High-volume servers do not always want every span exported. `SamplingSpanProcessor`
wraps your real exporter and decides, per call, whether to forward it:

```python
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from mcpobs import SamplingSpanProcessor

provider.add_span_processor(
    SamplingSpanProcessor(BatchSpanProcessor(exporter), mode="errors_only")
)
```

- `mode="all"` (default) — export everything. Identical to not wrapping at all.
- `mode="errors_only"` — export only calls that actually failed.
- `mode="percentage"` with `rate=0.1` — export failures always, plus 10% of
  successful calls.

**Failures are never sampled away**, in either mode. The decision is made after
a call finishes, not before — sampling before you know whether something is
about to fail would mean discarding traffic *before* deciding which slice would
have been the failures, which defeats the reason to have this product at all.

## Servers launched by the client (stdio)

If the client starts your server, it runs on your **user's** machine — so it
must not hold a long-lived credential. Your backend mints a short-lived token
instead:

```bash
pip install "mcpobs[sessions]"
```

```python
instrument(
    mcp,
    session_endpoint="https://yourapp.com/mcpobs-session",
    session_headers=lambda: {"authorization": f"Bearer {current_token()}"},
)
```

Pass a **callable** — the credential authenticating that call usually refreshes,
and a dict read once at startup would work for an hour and then quietly stop.

If your endpoint is unreachable, the server starts **without telemetry** and
retries. Observability must never be the reason a tool fails to start.

## Requirements

- Python 3.11+
- `opentelemetry-api`, `opentelemetry-sdk` (installed with this package)
- `mcpobs[sessions]` additionally installs the OTLP HTTP exporter and `httpx`

Licensed under Apache-2.0.
