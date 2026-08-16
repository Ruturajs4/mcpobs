"""OpenTelemetry bootstrap for the demo MCP server.

Principle (V2 18.1 / Architecture.md ADR-001): the MCP Python SDK v2 already
creates the MCP protocol spans -- `OpenTelemetryMiddleware` ships ON BY DEFAULT
(see mcp/server/lowlevel/server.py:433). Our job is *only* to configure a
TracerProvider and an exporter. We never wrap tool handlers and we never create
a second span per tool call.

Modes:
    otlp    -> BatchSpanProcessor + OTLP/HTTP  (the real path, needs Collector)
    console -> SimpleSpanProcessor + Console   (eyeballing)
    memory  -> SimpleSpanProcessor + InMemory  (in-process capture)
    file    -> SimpleSpanProcessor + NDJSON    (T3 capture from a SUBPROCESS,
                                                which is how stdio/http servers
                                                run -- no Docker needed)
"""

from __future__ import annotations

import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SERVICE_NAME = "mcp-demo-server"
SERVICE_VERSION = "0.1.0"
ENVIRONMENT = "local"

# 4319 -- the AUTHENTICATING GATEWAY, not the Collector on 4318. The demo
# server is a stand-in for a customer's server, and a customer has no route to
# the Collector at all: its OTLP ports are not published (docker-compose.yml).
DEFAULT_OTLP_ENDPOINT = "http://localhost:4319/v1/traces"

KEYS_FILE = "/.mcpobs-keys.env"


def _dev_key() -> str:
    """The local ingest key, from the gitignored env file `make devkeys` writes.

    Read here rather than required as an environment variable because the demo
    is run by hand constantly, and a step that must be remembered every time is
    a step that will be forgotten. A real customer sets
    OTEL_EXPORTER_OTLP_HEADERS themselves; that still wins if it is set.
    """
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / ".mcpobs-keys.env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "MCPOBS_INGEST_KEY":
            return value.strip()
    return ""

_provider: TracerProvider | None = None
_memory_exporter: InMemorySpanExporter | None = None


def init_telemetry(
    mode: str | None = None,
    endpoint: str | None = None,
) -> tuple[TracerProvider, InMemorySpanExporter | None]:
    """Configure the global TracerProvider. Idempotent within a process."""
    global _provider, _memory_exporter

    if _provider is not None:
        return _provider, _memory_exporter

    mode = (mode or os.getenv("OTEL_MODE", "otlp")).lower()

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": ENVIRONMENT,
        }
    )
    provider = TracerProvider(resource=resource)

    from mcpobs.exporter import SessionSpanExporter
    from mcpobs.session import SessionProvider

    if mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif mode == "file":
        path = os.getenv("OTEL_SPAN_FILE", "spans.ndjson")
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lives for process life
        provider.add_span_processor(
            SimpleSpanProcessor(
                ConsoleSpanExporter(
                    out=handle,
                    formatter=lambda span: span.to_json(indent=None) + "\n",
                )
            )
        )
    elif mode == "memory":
        _memory_exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_memory_exporter))
    else:
        # Imported lazily so `memory`/`console` modes work without the exporter installed.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # schedule_delay_millis is deliberately short. On Windows, subprocess
        # .terminate() is a hard kill (TerminateProcess) -- no SIGTERM, no
        # atexit, no flush -- so a demo server's last spans are lost unless the
        # processor has already exported them on its own schedule. Production
        # servers are long-lived and would use the default.
        headers = {}
        if not os.getenv("OTEL_EXPORTER_OTLP_HEADERS"):
            key = _dev_key()
            if key:
                headers["x-api-key"] = key

        # A session endpoint takes precedence over a static key (ADR-011). This
        # is the stdio case: the server runs on the END USER's machine, so it
        # must not hold a long-lived credential -- it fetches a 3-hour token
        # from an endpoint the customer hosts, and the exporter rotates it.
        target = endpoint or os.getenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", DEFAULT_OTLP_ENDPOINT
        )
        session_provider = SessionProvider()
        exporter: OTLPSpanExporter
        if session_provider.configured:
            # SAID OUT LOUD when both are configured. The session endpoint wins,
            # which is right -- it is the safer credential -- but silently
            # ignoring a key someone deliberately set is how they come to
            # believe it is a working fallback.
            #
            # It is NOT a fallback, deliberately. Falling back to a long-lived
            # org-wide key the moment the session endpoint blinked would undo
            # the entire point of ADR-011, on the machine least able to protect
            # it. So if that endpoint is unreachable, telemetry pauses and the
            # key is still not used -- and an operator has to be told that
            # BEFORE it happens, not deduced from an absence of spans.
            # The ENV VAR only, not `headers` -- which `_dev_key()` fills from
            # the gitignored local file whether or not anyone asked for it.
            # Warning about a key the developer never configured would train
            # them to ignore the warning that matters.
            if os.getenv("OTEL_EXPORTER_OTLP_HEADERS"):
                print(
                    "[mcpobs] both MCPOBS_SESSION_ENDPOINT and a static API key are "
                    "set. Using the session endpoint; the static key will NOT be "
                    "used, even if the endpoint is unreachable.",
                    file=sys.stderr,
                )
            exporter = SessionSpanExporter(session_provider, endpoint=target)
        else:
            # Both sources, for the mirror-image reason: when
            # OTEL_EXPORTER_OTLP_HEADERS is set, `_dev_key()` is skipped and
            # `headers` stays empty because OTel reads that variable itself. A
            # check on `headers` alone called a correctly-configured server
            # uncredentialed.
            if not headers.get("x-api-key") and not os.getenv("OTEL_EXPORTER_OTLP_HEADERS"):
                # Neither credential. Spans will be refused with 401 and the
                # only symptom is an empty console, which is a long way from the
                # cause.
                print(
                    "[mcpobs] no credential configured: set MCPOBS_SESSION_ENDPOINT "
                    "(recommended for stdio) or OTEL_EXPORTER_OTLP_HEADERS. "
                    "Telemetry will be rejected.",
                    file=sys.stderr,
                )
            exporter = OTLPSpanExporter(endpoint=target, headers=headers or None)

        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                schedule_delay_millis=int(os.getenv("OTEL_BSP_SCHEDULE_DELAY", "1000")),
            )
        )

    trace.set_tracer_provider(provider)
    _instrument_httpx()

    _provider = provider
    return provider, _memory_exporter


def _instrument_httpx() -> None:
    """Turn on downstream instrumentation the way a customer would.

    Uses `mcpobs.instrument_downstream()` rather than naming HTTPXClientInstrumentor
    and SQLite3Instrumentor by hand, so the demo exercises the code path the
    README tells customers to use. A demo that integrates differently from the
    documentation is how the documentation goes stale without anyone noticing.
    """
    from mcpobs import instrument_downstream, instrument_httpx

    # STDERR, never stdout. On the stdio transport stdout IS the JSON-RPC
    # channel, so a single stray line makes the client fail to parse the next
    # message. This printed to stdout and stayed silent for weeks because the
    # only branch that printed was the failure branch -- which nothing had
    # triggered until `opentelemetry-instrumentation-pymysql` was installed
    # without the `pymysql` driver beside it. A diagnostic that corrupts the
    # protocol it is diagnosing is worse than no diagnostic.
    for name, outcome in instrument_downstream().items():
        if outcome != "instrumented":
            print(f"[otel_bootstrap] {name}: {outcome}", file=sys.stderr)

    # Body capture on top (D60). Order-independent -- proved by
    # tests/test_control_plane.py, in both directions.
    if os.getenv("MCPOBS_HTTP_BODIES", "1") == "1":
        instrument_httpx()


def shutdown() -> None:
    """Flush pending spans. Required for short-lived processes."""
    if _provider is not None:
        _provider.shutdown()
