"""Server middleware that annotates the SDK's span with a failure kind.

DESIGN CONSTRAINT (V2 §18.1 / Architecture.md ADR-001)
    We do NOT wrap the MCP protocol and we do NOT create a span. The MCP SDK's
    `OpenTelemetryMiddleware` ships on by default and owns the `tools/call`
    span. This middleware runs *inside* it -- so that span is still open -- and
    adds exactly one attribute to it.

    That ordering is what makes this possible at all. From
    mcpserver/server.py:232: "User middleware runs inside the SDK's built-ins
    (OpenTelemetry, then the request-state boundary), outermost-first."

        OpenTelemetryMiddleware        <- span opens
          RequestStateBoundary
            FailureClassifierMiddleware  <- us: span open, result converted
              _handle_call_tool          <- exception -> isError happens here
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry.trace import get_current_span

from mcpobs.classifier import (
    ATTRIBUTE,
    CLASSIFIER_VERSION,
    RESULT_TYPE_ATTRIBUTE,
    FailureClassifier,
)

log = logging.getLogger(__name__)


class FailureClassifierMiddleware:
    """Adds `mcpobs.failure.kind` to the SDK's span when a tool call fails.

    Adds nothing on success, so the common path costs one boolean check.
    """

    def __init__(self, classifier: FailureClassifier | None = None) -> None:
        self.classifier = classifier or FailureClassifier()

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        try:
            self._annotate(result)
        except Exception as exc:  # noqa: BLE001
            # Telemetry enrichment must never break a customer's tool call.
            # Losing an attribute is an acceptable failure; losing the request
            # is not.
            log.debug("failure classification skipped: %s", exc)
        return result

    def _annotate(self, result: Any) -> None:
        span = get_current_span()
        if not span.is_recording():
            return

        # `resultType` is only interesting when it is NOT "complete". Setting it
        # on every span would cost an attribute on the overwhelmingly common
        # path to say nothing; absence means complete.
        result_type = self.classifier.result_type(result)
        if result_type and result_type != "complete":
            span.set_attribute(RESULT_TYPE_ATTRIBUTE, result_type)

        # An `input_required` result is an MRTR interim result, NOT a failure.
        # Classifying it would be the single most likely way to corrupt an
        # error rate (Day-1 doc §3.2).
        if result_type == "input_required":
            return

        if not self.classifier.is_error(result):
            return

        span.set_attribute(ATTRIBUTE, self.classifier.classify_result(result))
        span.set_attribute(f"{ATTRIBUTE}.version", CLASSIFIER_VERSION)
