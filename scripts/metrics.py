"""Readable summary of the pipeline's own metrics (V2 §19).

Raw Prometheus exposition is unreadable at a glance -- every series carries ten
resource labels. This renders the handful of numbers someone would actually look
at during an incident.

    python scripts/metrics.py
"""

from __future__ import annotations

import re
import sys
import urllib.request

ENDPOINT = "http://localhost:8889/metrics"

SERIES = re.compile(r"^(?P<name>mcpobs_[a-z_]+)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$")


def scrape(url: str = ENDPOINT) -> dict[str, list[tuple[dict[str, str], float]]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8")
    except Exception as exc:
        print(f"could not scrape {url}: {exc}", file=sys.stderr)
        return {}

    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in body.splitlines():
        match = SERIES.match(line.strip())
        if not match:
            continue
        labels = dict(
            re.findall(r'(\w+)="([^"]*)"', match.group("labels"))
        )
        out.setdefault(match.group("name"), []).append((labels, float(match.group("value"))))
    return out


def total(series: dict, name: str) -> float:
    return sum(value for _, value in series.get(name, []))


def histogram(series: dict, prefix: str) -> tuple[float, float]:
    """Return (count, mean_ms) for an OTel histogram exported to Prometheus."""
    count = total(series, f"{prefix}_count")
    summed = total(series, f"{prefix}_sum")
    return count, (summed / count if count else 0.0)


def by_label(series: dict, name: str, label: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for labels, value in series.get(name, []):
        out[labels.get(label, "?")] = out.get(labels.get(label, "?"), 0.0) + value
    return out


def main() -> int:
    series = scrape()
    if not series:
        print("no mcpobs metrics yet -- run `make demo`, then wait for the 15s export interval")
        return 0

    spans = total(series, "mcpobs_normalizer_spans_total")
    rows = total(series, "mcpobs_normalizer_rows_inserted_total")
    batches = total(series, "mcpobs_normalizer_batches_committed_total")
    failures = total(series, "mcpobs_normalizer_insert_failures_total")
    dlq = by_label(series, "mcpobs_normalizer_dead_lettered_total", "reason")

    insert_n, insert_mean = histogram(series, "mcpobs_normalizer_insert_duration_milliseconds")
    fresh_n, fresh_mean = histogram(series, "mcpobs_normalizer_freshness_milliseconds")

    print("PIPELINE SELF-TELEMETRY")
    print("-" * 52)
    print(f"  spans normalized       {spans:>12,.0f}")
    print(f"  rows inserted          {rows:>12,.0f}")
    print(f"  batches committed      {batches:>12,.0f}")
    print(f"  insert failures        {failures:>12,.0f}"
          + ("   <-- INVESTIGATE" if failures else ""))
    print()
    print(f"  insert latency mean    {insert_mean:>12,.1f} ms   (n={insert_n:,.0f})")
    print(f"  freshness mean         {fresh_mean / 1000:>12,.1f} s    (n={fresh_n:,.0f})")
    print("      ^ event time -> write time. Its floor is the batch interval,")
    print("        so this number is mostly BATCH_MAX_SECONDS by design.")
    print()
    if dlq:
        print("  dead-lettered by reason:")
        for reason, count in sorted(dlq.items(), key=lambda kv: -kv[1]):
            print(f"      {reason:<24} {count:,.0f}")
    else:
        print("  dead-lettered            none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
