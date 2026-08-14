from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from normalizer.models import DecodedSpan  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def otlp_payload() -> bytes:
    """A real OTLP batch captured off otlp.spans.raw, not a synthetic one."""
    return (FIXTURES / "otlp_batch.bin").read_bytes()


def make_span(**overrides) -> DecodedSpan:
    """A minimal MCP tools/call span; override attributes per test."""
    attrs = {
        "mcp.method.name": "tools/call",
        "mcp.protocol.version": "2026-07-28",
        "jsonrpc.request.id": "1",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "echo_fast",
    }
    attrs.update(overrides.pop("span_attributes", {}))
    base = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "span_name": "tools/call echo_fast",
        "span_kind": "SERVER",
        "start_unix_nano": int(datetime(2026, 8, 14, 12, 0).timestamp() * 1e9),
        "duration_ns": 1_500_000,
        "status_code": "UNSET",
        "resource_attributes": {
            "service.name": "mcp-demo-server",
            "service.version": "0.1.0",
            "deployment.environment.name": "local",
            "tenant.id": "local",
            "project.id": "local",
        },
        "span_attributes": attrs,
    }
    base.update(overrides)
    return DecodedSpan(**base)


@pytest.fixture
def span_factory():
    return make_span
