"""The normalizer's own telemetry (V2 §19).

An observability product that cannot observe itself is a liability: queue lag,
dropped telemetry and ClickHouse insert failures are all product-impacting
incidents, and until now the only way to know the pipeline was healthy was to
run `make verify` by hand.

WHY METRICS AND NOT TRACES
    Tracing the normalizer would mean emitting spans about the act of storing
    spans. The volume scales 1:1 with customer traffic and the feedback loop is
    real: our telemetry would become our own largest tenant. Metrics are
    bounded by cardinality instead of by traffic, which is the right shape for
    a hot loop.

WHERE IT GOES
    OTLP to the Collector's METRICS pipeline, which is entirely separate from
    the traces pipeline that feeds Kafka. Self-telemetry must never share a
    path with customer telemetry -- if it did, the outage that broke ingestion
    would also blind us to the outage.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

SERVICE_NAME = "mcpobs-normalizer"


class NullInstrument:
    """No-op stand-in so the hot loop never branches on whether telemetry loaded."""

    def add(self, *args: Any, **kwargs: Any) -> None: ...
    def record(self, *args: Any, **kwargs: Any) -> None: ...


class PipelineMetrics:
    """Instruments for the ingest path.

    Deliberately small. Every metric here answers a question someone would ask
    during an incident; anything that does not is noise with a storage bill.
    """

    def __init__(self, endpoint: str | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.spans_normalized: Any = NullInstrument()
        self.rows_inserted: Any = NullInstrument()
        self.insert_duration: Any = NullInstrument()
        self.insert_failures: Any = NullInstrument()
        self.dead_lettered: Any = NullInstrument()
        self.batches_committed: Any = NullInstrument()
        self.freshness: Any = NullInstrument()

        if enabled:
            self._install(endpoint)

    def _install(self, endpoint: str | None) -> None:
        try:
            from opentelemetry import metrics
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
        except ImportError as exc:  # pragma: no cover - optional dependency
            log.warning("self-telemetry disabled (missing OTel SDK): %s", exc)
            return

        try:
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=endpoint
                    or os.getenv(
                        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
                        "http://otel-collector:4318/v1/metrics",
                    )
                ),
                export_interval_millis=int(os.getenv("OTEL_METRIC_INTERVAL_MS", "15000")),
            )
            provider = MeterProvider(
                resource=Resource.create(
                    {
                        "service.name": SERVICE_NAME,
                        "service.version": os.getenv("NORMALIZER_VERSION", "0.1.0"),
                        "deployment.environment.name": os.getenv("ENVIRONMENT", "local"),
                    }
                ),
                metric_readers=[reader],
            )
            metrics.set_meter_provider(provider)
            meter = metrics.get_meter(SERVICE_NAME)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break ingest
            log.warning("self-telemetry disabled: %s", exc)
            return

        self.spans_normalized = meter.create_counter(
            "mcpobs.normalizer.spans", unit="{span}", description="Spans decoded and normalized"
        )
        self.rows_inserted = meter.create_counter(
            "mcpobs.normalizer.rows_inserted",
            unit="{row}",
            description="Rows written to ClickHouse",
        )
        self.insert_duration = meter.create_histogram(
            "mcpobs.normalizer.insert.duration",
            unit="ms",
            description="ClickHouse insert latency",
        )
        self.insert_failures = meter.create_counter(
            "mcpobs.normalizer.insert.failures",
            unit="{failure}",
            description="Failed ClickHouse inserts",
        )
        self.dead_lettered = meter.create_counter(
            "mcpobs.normalizer.dead_lettered",
            unit="{message}",
            description="Messages routed to the DLQ, by reason",
        )
        self.batches_committed = meter.create_counter(
            "mcpobs.normalizer.batches_committed",
            unit="{batch}",
            description="Batches whose offsets were committed after a successful insert",
        )
        # THE headline metric (Architecture.md §9.1): event time -> queryable
        # time. Recorded per batch from the spans actually written, so it
        # reflects the real path rather than a synthetic probe.
        self.freshness = meter.create_histogram(
            "mcpobs.normalizer.freshness",
            unit="ms",
            description="End-to-end freshness: span event time to write time",
        )
        log.info("self-telemetry enabled -> %s", SERVICE_NAME)
