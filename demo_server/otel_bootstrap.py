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

DEFAULT_OTLP_ENDPOINT = "http://localhost:4318/v1/traces"

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
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint
                    or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", DEFAULT_OTLP_ENDPOINT)
                ),
                schedule_delay_millis=int(os.getenv("OTEL_BSP_SCHEDULE_DELAY", "1000")),
            )
        )

    trace.set_tracer_provider(provider)
    _instrument_httpx()

    _provider = provider
    return provider, _memory_exporter


def _instrument_httpx() -> None:
    """Auto-instrument httpx so downstream calls appear as child spans.

    NOTE: the MCP SDK itself ships `httpx2`, which this instrumentor does NOT
    patch. The demo server therefore makes its downstream calls with plain
    `httpx` on purpose -- otherwise assertion A4 (downstream span parented to
    the MCP span) would silently never fire.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover - instrumentation is best-effort
        print(f"[otel_bootstrap] httpx instrumentation unavailable: {exc}")

    try:
        from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor

        SQLite3Instrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        print(f"[otel_bootstrap] sqlite3 instrumentation unavailable: {exc}")


def shutdown() -> None:
    """Flush pending spans. Required for short-lived processes."""
    if _provider is not None:
        _provider.shutdown()
