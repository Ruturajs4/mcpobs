"""Ingest quotas: soft flags and hard rejections (Architecture.md §5.1, ADR-008).

WHERE IT SITS AND WHY
    §5.1's write path puts the quota check between authentication and stamping,
    on the near side of the ack boundary. That position is the whole point: a
    rejection must happen before we have promised to keep anything. Checking
    after the ack would mean telling a customer their data was safe and then
    dropping it, which ADR-008 forbids in as many words -- "never ack data we
    might drop".

WHAT IS METERED, AND WHY IT IS SPANS
    Spans, not requests. A request can carry one span or ten thousand, so
    metering requests would let a customer send the same volume in a hundredth
    of the calls and stay under any limit. Spans are also what the pipeline
    actually costs money to store, so they are the honest unit.

TWO WINDOWS, BECAUSE THEY CATCH DIFFERENT THINGS
    A per-minute rate limit catches the whale flooding ingest -- the failure
    mode in §8, where one tenant's burst becomes every other tenant's consumer
    lag. A per-day volume limit catches sustained overuse that no single minute
    would notice. Neither subsumes the other.

FAIL OPEN, DELIBERATELY
    If the counter store is unavailable we ALLOW the request. Architecture §8
    already settles this shape of question: with the control plane down,
    "Redis-cached keys keep ingest working". Refusing a customer's telemetry
    because OUR bookkeeping is broken inverts who is being protected -- and an
    ingest path that fails when a cache fails is exactly the fragility ADR-003
    was written to avoid. An over-quota tenant getting a free minute during our
    outage is the cheaper mistake.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plan:
    """Limits for a plan. `0` means unlimited."""

    name: str
    spans_per_minute: int
    spans_per_day: int

    def unlimited(self) -> bool:
        return self.spans_per_minute == 0 and self.spans_per_day == 0


#: Deliberately small and in code rather than a table. These are product
#: decisions that change on a release cadence, not per-tenant data; a per-org
#: OVERRIDE is what belongs in the database, and 002_quotas.sql adds exactly
#: that. A plans table would have invited someone to edit limits in prod SQL.
PLANS: Final[dict[str, Plan]] = {
    "trial": Plan("trial", spans_per_minute=2_000, spans_per_day=200_000),
    "pro": Plan("pro", spans_per_minute=50_000, spans_per_day=20_000_000),
    "enterprise": Plan("enterprise", spans_per_minute=0, spans_per_day=0),
    "local": Plan("local", spans_per_minute=0, spans_per_day=0),
}

DEFAULT_PLAN: Final = "trial"

SOFT_FRACTION: Final = 0.8
"""Flag at 80% of a limit.

The point of a soft threshold is that the customer hears about it BEFORE
anything is refused. A flag raised at 100% would arrive at the same moment as
the rejection and tell them nothing they were not about to find out.
"""


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    soft_exceeded: bool = False
    reason: str = ""
    retry_after: int = 0
    #: What the counter says now, for the operator reading a rejection.
    used_minute: int = 0
    used_day: int = 0
    limit_minute: int = 0
    limit_day: int = 0

    ALLOW: Final = "allow"


class QuotaStore:
    """Redis-backed counters. Falls back to in-process when Redis is absent.

    THE FALLBACK IS NOT EQUIVALENT AND SAYS SO. An in-process counter meters one
    gateway replica, so a fleet of four enforces four times the limit. It is
    correct for the local rung and for a single instance, and it is wrong the
    moment the gateway scales -- which is exactly the sort of difference that
    should be stated rather than discovered. Redis is in the target architecture
    (§3) precisely for shared state like this.
    """

    def __init__(self, url: str | None = None) -> None:
        self.url: str = url or os.environ.get("REDIS_URL", "redis://redis:6379/0")
        self._client: Any = None
        self._local: dict[str, tuple[float, int]] = {}
        self.degraded = False

    @property
    def client(self) -> Any:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self.url, socket_timeout=0.25, socket_connect_timeout=0.25
            )
        return self._client

    def incr(self, key: str, amount: int, ttl: int) -> int:
        """Add `amount` to `key`, expiring after `ttl` seconds. Returns the total.

        The timeouts above are 250ms on purpose: this runs on the ingest hot
        path, and a slow counter must degrade to no counter rather than to slow
        ingest.
        """
        try:
            pipe = self.client.pipeline()
            pipe.incrby(key, amount)
            pipe.expire(key, ttl)
            total = int(pipe.execute()[0])
            self.degraded = False
            return total
        except Exception as exc:  # noqa: BLE001
            if not self.degraded:
                log.warning("quota counter unavailable, counting in-process: %s", exc)
                self.degraded = True
            return self._incr_local(key, amount, ttl)

    def _incr_local(self, key: str, amount: int, ttl: int) -> int:
        now = time.monotonic()
        expiry, count = self._local.get(key, (0.0, 0))
        if expiry < now:
            expiry, count = now + ttl, 0
        count += amount
        self._local[key] = (expiry, count)
        if len(self._local) > 10_000:
            # Bounded. Keys are time-windowed so they expire naturally; this is
            # the guard for a process that has been up a very long time.
            self._local = {k: v for k, v in self._local.items() if v[0] >= now}
        return count


class QuotaEnforcer:
    def __init__(self, store: QuotaStore | None = None) -> None:
        self.store = store or QuotaStore()

    @staticmethod
    def plan_for(name: str) -> Plan:
        return PLANS.get(name or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])

    def check(
        self,
        tenant: str,
        plan_name: str,
        spans: int,
        override_minute: int | None = None,
        override_day: int | None = None,
    ) -> Verdict:
        """Count `spans` against `tenant`'s limits and rule on the request.

        Counts BEFORE deciding, deliberately. Deciding first and counting only
        what was accepted would make a rejected burst invisible in the counters,
        so the tenant that is being rejected would look quiet -- and the operator
        investigating would see no evidence of the flood.
        """
        plan = self.plan_for(plan_name)
        limit_minute = override_minute if override_minute is not None else plan.spans_per_minute
        limit_day = override_day if override_day is not None else plan.spans_per_day
        if not spans or (limit_minute == 0 and limit_day == 0):
            return Verdict(allowed=True, limit_minute=limit_minute, limit_day=limit_day)

        now = datetime.now(UTC)
        used_minute = self.store.incr(
            f"q:{tenant}:m:{now:%Y%m%d%H%M}", spans, ttl=120
        )
        used_day = self.store.incr(f"q:{tenant}:d:{now:%Y%m%d}", spans, ttl=172_800)

        if limit_minute and used_minute > limit_minute:
            return Verdict(
                allowed=False,
                reason=f"rate limit: {used_minute} spans this minute, limit {limit_minute}",
                # To the top of the next minute, so a client retrying at that
                # point finds a fresh window instead of being refused again.
                retry_after=max(1, 60 - now.second),
                used_minute=used_minute, used_day=used_day,
                limit_minute=limit_minute, limit_day=limit_day,
            )
        if limit_day and used_day > limit_day:
            return Verdict(
                allowed=False,
                reason=f"daily volume: {used_day} spans today, limit {limit_day}",
                # Retrying inside the day cannot help, so the hint is the reset.
                retry_after=max(1, 86_400 - (now.hour * 3600 + now.minute * 60 + now.second)),
                used_minute=used_minute, used_day=used_day,
                limit_minute=limit_minute, limit_day=limit_day,
            )

        soft = bool(
            (limit_minute and used_minute > limit_minute * SOFT_FRACTION)
            or (limit_day and used_day > limit_day * SOFT_FRACTION)
        )
        return Verdict(
            allowed=True, soft_exceeded=soft,
            used_minute=used_minute, used_day=used_day,
            limit_minute=limit_minute, limit_day=limit_day,
        )
