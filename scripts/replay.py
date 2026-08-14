"""Reprocess telemetry from Kafka after a normalizer fix.

This is the capability that justifies the entire Kafka tier (ADR-007). A
normalizer bug is recoverable without asking a single customer to resend data
they no longer have.

    python scripts/replay.py --group replay-2026-08-15 --from earliest

Uses its OWN consumer group, so the live normalizer keeps running and its
offsets are untouched -- the independent-consumer-group invariant in
Architecture.md §6.4.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from normalizer.config import Settings
from normalizer.consumer import Normalizer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, help="a NEW consumer group id")
    parser.add_argument("--bootstrap", default=None, help="defaults to the host listener")
    parser.add_argument("--idle", type=float, default=8.0, help="seconds of silence = drained")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stdout)

    base = Settings()
    settings = base.model_copy(
        update={
            "kafka_group_id": args.group,
            "kafka_bootstrap": args.bootstrap or base.kafka_host_bootstrap,
        }
    )

    normalizer = Normalizer(settings=settings, stop_when_idle=args.idle)
    print(f"replaying {settings.kafka_topic} as group={args.group} from earliest")
    normalizer.run()
    print(f"replayed {normalizer.span_count} spans")
    return 0


if __name__ == "__main__":
    sys.exit(main())
