# Payloads and privacy

The short version: **error classification never requires your payloads**, and
payload capture is a separate, explicit opt-in.

## What leaves your process by default

| | Captured by default |
| --- | --- |
| Tool name, method, protocol version | Yes |
| Duration, status, failure category | Yes |
| Downstream system, operation, host, status | Yes |
| SQL statement | Yes — **redacted** |
| Error text on failing calls | Yes — truncated, see below |
| Tool arguments and results | **No** |
| HTTP request bodies and headers | **No** — `instrument_httpx()` opts in |

## Error detail

On **failing calls only**, the classifier captures a truncated error message so
the console can show you *why* something failed rather than only *that* it did.
Boundaries, all enforced in code:

- Failing spans only. A successful result is never read for detail.
- Truncated, with a marker when cut.
- Stored in its own field — never the payload fields, which stay empty unless
  you explicitly enable payload capture.

Turn it off:

```python
instrument(mcp, capture_error_detail=False)
```

This is on by default, which is a deliberate trade: without it, most failures
carry no message anywhere in the telemetry and first-run debugging is
substantially worse. It is disclosed here rather than in a privacy appendix
precisely because it is on by default.

## Outbound HTTP bodies

`instrument_httpx()` records the request body, and request and response headers
filtered to an **allow-list** (`content-type`, `user-agent`, `traceparent`, …).

`authorization`, `cookie` and friends are **never read at all**, rather than
read and then scrubbed — so a header nobody anticipated is excluded by default
rather than captured by default.

!!! info "There is no response body, and that is a limitation rather than a setting"

    OpenTelemetry's HTTP instrumentation wraps the *transport*, so its span ends
    when the transport returns — and httpx reads the response body after that.
    Capturing one would mean buffering every response inside the hook, turning
    any streaming download in your process into a full in-memory read to satisfy
    telemetry. The console says so on the span instead of showing a blank.

## Redaction

Everything is redacted at **normalize time, before storage** — never at render.
A secret that reaches the table cannot be recalled. Redaction covers every
attribute, not a fixed list of field names, so a token in an unexpected place is
still scrubbed.
