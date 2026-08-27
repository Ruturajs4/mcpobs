"""Turn on every OpenTelemetry instrumentation the customer has installed.

WHY THIS EXISTS WHEN OTEL ALREADY HAS AN ANSWER
    OTel's own answer is the auto-instrumentation agent:

        opentelemetry-instrument python -m your_server

    That is the better path when it is available, and the README says so first.
    We maintain nothing, and it picks up a new instrumentation package the day
    the customer installs it.

    It is not always available. A large share of MCP servers are SPAWNED BY THE
    CLIENT over stdio -- Claude Desktop, an IDE, another agent -- and the
    customer does not own the command line that starts their process. There is
    no `opentelemetry-instrument` to prepend to a command someone else writes.
    For those servers a one-call, in-process equivalent is the only option, and
    that case is common enough in MCP specifically that this earns its place.

WHY IT DISCOVERS RATHER THAN LISTS
    Every instrumentation package registers itself under the
    `opentelemetry_instrumentor` entry-point group -- that is the mechanism the
    agent itself uses. Reading it means `pip install
    opentelemetry-instrumentation-redis` is the whole integration: no mcpobs
    release, no name in a list here that we would forget to add.

WHY IT IS STILL OPT-IN
    `instrument(server)` does NOT call this. Patching a customer's database
    driver as a side effect of "observe my MCP server" is not a thing to do
    quietly, and it is the same reasoning that keeps `instrument_httpx()` a
    separate call (D60).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import Final

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP: Final = "opentelemetry_instrumentor"

INSTRUMENTED: Final = "instrumented"
"""Outcome: the library is now emitting spans."""


def available() -> tuple[str, ...]:
    """Names of the instrumentations installed in this process.

    Separate from `instrument_downstream` so a customer can ask what WOULD be
    turned on before turning it on. "It silently did nothing" and "it silently
    did more than I expected" are both worth being able to check.
    """
    return tuple(sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)))


def instrument_downstream(exclude: Iterable[str] = ()) -> dict[str, str]:
    """Instrument every installed library. Returns {name: outcome}.

    A REPORT, not None. The customer needs to be able to see what this touched
    -- a call that patches an unknown set of libraries and says nothing is not
    something anyone should be comfortable putting in a production server.

    Never raises. Each instrumentor is attempted independently, so one package
    with a version conflict cannot stop the others, and none of them can take
    down the customer's startup. An observability library that prevents a server
    from booting has done more damage than the telemetry was worth.
    """
    skip = set(exclude)
    outcomes: dict[str, str] = {}

    for entry_point in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        name = entry_point.name
        if name in skip:
            outcomes[name] = "skipped: excluded"
            continue
        try:
            instrumentor_class = entry_point.load()
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = f"skipped: will not load ({exc})"
            continue
        try:
            instrumentor = instrumentor_class()
            # OTel's BaseInstrumentor is a singleton and `instrument()` is a
            # no-op once active, so this cannot clobber hooks attached by an
            # earlier `instrument_httpx()`. Order between the two is therefore
            # free -- which is worth knowing, because a customer will get it
            # wrong half the time and nothing should depend on it.
            if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
                outcomes[name] = "already instrumented"
                continue
            instrumentor.instrument()
            outcomes[name] = INSTRUMENTED
        except Exception as exc:  # noqa: BLE001
            # The common case is a DependencyConflict: the installed library
            # version is outside what the instrumentation supports. That is
            # information, not a failure, so it is reported rather than raised.
            outcomes[name] = f"skipped: {type(exc).__name__}: {exc}"

    instrumented = [n for n, outcome in outcomes.items() if outcome == INSTRUMENTED]
    log.info(
        "downstream instrumentation: %s",
        ", ".join(instrumented) if instrumented else "nothing installed",
    )
    return outcomes
