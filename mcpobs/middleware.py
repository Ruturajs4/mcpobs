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
    DETAIL_ATTRIBUTE,
    MRTR_STATE_ATTRIBUTE,
    REQUEST_ATTRIBUTE,
    REQUEST_SIZE_ATTRIBUTE,
    RESOURCE_URI_ATTRIBUTE,
    RESPONSE_ATTRIBUTE,
    RESPONSE_SIZE_ATTRIBUTE,
    RESULT_TYPE_ATTRIBUTE,
    FailureClassifier,
)
from mcpobs.payload import PayloadCapture

log = logging.getLogger(__name__)


class FailureClassifierMiddleware:
    """Adds `mcpobs.failure.kind` to the SDK's span when a tool call fails.

    Adds nothing on success, so the common path costs one boolean check.
    """

    def __init__(
        self,
        classifier: FailureClassifier | None = None,
        capture_error_detail: bool = True,
        capture_payloads: bool = False,
        payload_capture: PayloadCapture | None = None,
    ) -> None:
        self.classifier = classifier or FailureClassifier()
        self.capture_error_detail = capture_error_detail
        self.capture_payloads = capture_payloads
        self.payloads = payload_capture or PayloadCapture()

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        try:
            self._annotate(result, getattr(ctx, "params", None), ctx)
        except Exception as exc:  # noqa: BLE001
            # Telemetry enrichment must never break a customer's tool call.
            # Losing an attribute is an acceptable failure; losing the request
            # is not.
            log.debug("failure classification skipped: %s", exc)
        return result

    def _capture_payload(self, span: Any, ctx: Any, params: Any, result: Any) -> None:
        method = getattr(ctx, "method", "") or ""
        request_id = getattr(ctx, "request_id", None)
        request, request_size = self.payloads.request(method, request_id, params)
        if request:
            span.set_attribute(REQUEST_ATTRIBUTE, request)
            span.set_attribute(REQUEST_SIZE_ATTRIBUTE, request_size)
        response, response_size = self.payloads.response(request_id, result)
        if response:
            span.set_attribute(RESPONSE_ATTRIBUTE, response)
            span.set_attribute(RESPONSE_SIZE_ATTRIBUTE, response_size)

    def _annotate(self, result: Any, params: Any = None, ctx: Any = None) -> None:
        span = get_current_span()
        if not span.is_recording():
            return

        # Tool request/response. OFF by default (mcpobs/payload.py): unlike
        # error detail this is every argument and every result of every call.
        # Recorded BEFORE the success/error returns below, because the whole
        # point is to see what a successful-but-wrong call actually returned.
        if self.capture_payloads:
            self._capture_payload(span, ctx, params, result)

        # Which resource was read. The SDK records nothing for resources/*
        # because it derives its target from params["name"], and resources are
        # addressed by `uri` instead.
        resource_uri = self.classifier.resource_uri(params)
        if resource_uri:
            span.set_attribute(RESOURCE_URI_ATTRIBUTE, resource_uri)

        # MRTR correlation. A round that ASKS emits requestState; the next round
        # RECEIVES the same blob echoed back. Stamping both sides lets a query
        # chain the rounds of one logical call -- which trace_id cannot do,
        # because the rounds are separate traces (D28).
        for attribute, value in (
            (f"{MRTR_STATE_ATTRIBUTE}.out", self.classifier.outgoing_state(result)),
            (f"{MRTR_STATE_ATTRIBUTE}.in", self.classifier.incoming_state(params)),
        ):
            if value:
                span.set_attribute(attribute, value)

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

        # Only reached for a FAILING result -- every early return above has
        # already fired for success and for MRTR interim rounds. That ordering
        # is the "errors only" guarantee, and it is why this is the last thing
        # in the method rather than a branch somewhere in the middle.
        if self.capture_error_detail:
            detail = self.classifier.error_detail(result)
            if detail:
                span.set_attribute(DETAIL_ATTRIBUTE, detail)
