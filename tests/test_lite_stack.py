"""The lite (self-host) deployment, against the real running stack.

REQUIRES `docker-compose.lite.yml` UP. Skips loudly when it is not, matching
tests/test_browser_flows.py's convention -- a guard that reports green while
testing nothing is worse than no guard, and that mistake has already happened
once in this project (a `shutil.which` check silently disabled every stdio
test).

WHY THIS FILE IS SEPARATE FROM tests/test_direct_intake.py. That file proves
`DirectIntake` calls the right things with the right arguments, against a fake
ClickHouse client -- fast, and it can prove a token is a pure function of the
bytes. It CANNOT prove ClickHouse actually deduplicates on a repeated token, or
that ingest's lifespan actually applies every migration with no normalizer
container running, or that a byte reaches spans_raw and back out through the
query API with zero Kafka process anywhere on the host. Those are the claims
this file measures live, matching this project's established pattern
(tests/test_stdio_transport.py, tests/test_transport_attribution.py): assert
on what actually happened, not on what the code was supposed to do.

Run with: `make up-lite && make devkeys-lite && pytest tests/test_lite_stack.py -v`
"""

from __future__ import annotations

import os
import pathlib
import socket
import time
import urllib.error
import urllib.request
import uuid

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

INGEST = os.getenv("MCPOBS_LITE_INGEST_URL", "http://localhost:4319")
QUERY = os.getenv("MCPOBS_LITE_QUERY_URL", "http://localhost:8080")
KEY_FILE = pathlib.Path(__file__).parents[1] / ".mcpobs-keys.env"


def _read_keys_env() -> dict[str, str]:
    if not KEY_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_KEYS = _read_keys_env()
INGEST_KEY = os.getenv("MCPOBS_INGEST_KEY") or _KEYS.get("MCPOBS_INGEST_KEY", "")
READ_KEY = os.getenv("MCPOBS_READ_KEY") or _KEYS.get("MCPOBS_READ_KEY", "")


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def _kafka_reachable() -> bool:
    """True only if something is actually listening on Kafka's default port.

    The point of this file is proving the LITE stack works with no Kafka
    anywhere -- if the full stack happens to also be up (a port clash
    docker-compose.lite.yml's own header warns against), that is worth failing
    loudly on rather than silently testing the wrong deployment.
    """
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("localhost", 9092)) == 0


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (INGEST_KEY and READ_KEY),
        reason="no keys: run `make devkeys-lite` against the lite stack's Postgres",
    ),
    pytest.mark.skipif(
        not _http_ok(f"{INGEST}/ready"), reason=f"lite ingest not reachable at {INGEST}"
    ),
    pytest.mark.skipif(
        _kafka_reachable(),
        reason="kafka is reachable on :9092 -- this looks like the FULL stack, "
        "not lite (docker-compose.lite.yml has no kafka service at all)",
    ),
]


def _otlp_payload(tool_name: str) -> bytes:
    """One minimal, valid OTLP export request carrying a single MCP span.

    Sets `mcp.method.name`/`gen_ai.tool.name`, the attributes
    SpanNormalizer.to_row promotes into `mcp_method`/`mcp_tool_name`
    (normalizer/normalize.py) -- the trace-list DTO exposes THOSE columns,
    not the raw OTel span name, so a probe span without them would be
    unfindable via the query API regardless of whether ingest worked.
    """
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    scope_spans = resource_spans.scope_spans.add()
    span = scope_spans.spans.add()
    span.trace_id = uuid.uuid4().bytes
    span.span_id = uuid.uuid4().bytes[:8]
    span.name = f"tools/call {tool_name}"
    span.attributes.append(
        KeyValue(key="mcp.method.name", value=AnyValue(string_value="tools/call"))
    )
    span.attributes.append(
        KeyValue(key="gen_ai.tool.name", value=AnyValue(string_value=tool_name))
    )
    now_ns = int(time.time() * 1e9)
    span.start_time_unix_nano = now_ns
    span.end_time_unix_nano = now_ns + 1_000_000
    return request.SerializeToString()


def _post(payload: bytes) -> httpx.Response:
    return httpx.post(
        f"{INGEST}/v1/traces",
        content=payload,
        headers={"x-api-key": INGEST_KEY, "content-type": "application/x-protobuf"},
        timeout=15,
    )


def _find_trace(tool_name: str, *, attempts: int = 10, delay: float = 1.0) -> dict | None:
    """Poll the query API rather than sleeping a fixed amount -- the whole
    point of dropping Kafka is that there is no batching delay left to wait
    out, and a fixed sleep would hide a regression that reintroduces one."""
    for _ in range(attempts):
        response = httpx.get(
            f"{QUERY}/api/v1/traces",
            params={"limit": 50, "window_minutes": 60},
            headers={"x-api-key": READ_KEY},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
        for trace in items:
            if trace.get("tool") == tool_name:
                return trace
        time.sleep(delay)
    return None


class TestNoKafkaAnywhere:
    def test_the_stack_this_file_is_testing_really_has_no_kafka(self) -> None:
        """The guard the pytestmark skip already enforces, restated as an
        assertion so a future refactor of the skip logic cannot silently drop
        it and have this file start testing the full stack instead."""
        assert not _kafka_reachable()


class TestRealRoundTrip:
    """Decode -> normalize -> insert -> queryable, with no broker between
    ingest and ClickHouse. This is the claim the whole lite mode rests on."""

    def test_a_real_otlp_batch_reaches_clickhouse_and_is_queryable(self) -> None:
        tool_name = f"lite-probe-{uuid.uuid4().hex[:12]}"
        response = _post(_otlp_payload(tool_name))
        assert response.status_code == 200, response.text

        trace = _find_trace(tool_name)
        assert trace is not None, (
            f"tool {tool_name!r} never appeared via the query API -- direct_intake "
            "did not reach spans_raw, or the query path cannot see it"
        )

    def test_the_response_is_a_valid_otlp_export_response(self) -> None:
        """ingest returns an empty ExportTraceServiceResponse on the lite
        path (ingest/app.py), not an arbitrary body -- a spec-following OTel
        exporter parses this the same way it parses the Collector's real
        response."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceResponse,
        )

        response = _post(_otlp_payload(f"lite-probe-{uuid.uuid4().hex[:12]}"))
        assert response.status_code == 200
        ExportTraceServiceResponse().ParseFromString(response.content)  # raises if malformed


class TestDedupToken:
    """ADR-006, live: an identical retry of the same POST must not double
    the stored spans. tests/test_direct_intake.py proves the token is stable
    across identical bytes with a fake client; this proves ClickHouse
    actually honors it."""

    def test_resubmitting_the_identical_payload_does_not_duplicate(self) -> None:
        tool_name = f"lite-dedup-{uuid.uuid4().hex[:12]}"
        payload = _otlp_payload(tool_name)

        first = _post(payload)
        second = _post(payload)  # byte-identical retry
        assert first.status_code == 200
        assert second.status_code == 200

        trace = _find_trace(tool_name)
        assert trace is not None
        assert trace.get("span_count") == 1, (
            f"expected exactly one span for a byte-identical resubmission, "
            f"got span_count={trace.get('span_count')}"
        )


class TestMalformedPayloadRejected:
    def test_garbage_bytes_are_rejected_with_400_not_500(self) -> None:
        response = httpx.post(
            f"{INGEST}/v1/traces",
            content=b"this is not an OTLP payload",
            headers={"x-api-key": INGEST_KEY, "content-type": "application/x-protobuf"},
            timeout=10,
        )
        assert response.status_code == 400
