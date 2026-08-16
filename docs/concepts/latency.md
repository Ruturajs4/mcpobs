# Latency and clock accuracy

## Percentiles are computed over eligible spans only

Stream lifetimes and awaiting-input rounds are excluded at write time, not at
query time, so a reader cannot forget to exclude them.

## When a percentile is not a percentile

OpenTelemetry timestamps spans using the host clock. On Linux that is
nanosecond-grade. **On Windows the default timer granularity can be around half
a millisecond**, which is the same order as a fast tool call — so the
"percentiles" become a histogram of clock ticks rather than of latency.

We measure the actual tick on your host rather than assuming one, and the
console marks every percentile it cannot support:

> clock ticks every 0.509ms — p50 is within 10× of that, so these percentiles
> are quantisation, not latency

Two independent checks, because neither subsumes the other:

- A p50 within 10× of the observed tick is quantisation noise, however few
  zero-duration samples there are.
- A p50 comfortably above the tick still misleads when most samples measured
  exactly zero, because the surviving non-zero samples are a biased tail.

!!! tip "If you see this caveat"

    Your numbers are not wrong so much as under-resolved. Tool calls that do
    real work — a database query, an outbound API call — measure fine on any
    host. It is sub-millisecond in-process tools that a coarse clock cannot
    resolve.

## Freshness

The console shows **ingest lag**: how long a span takes to travel from happening
in your server to being queryable. Under a minute is healthy. It is displayed
rather than assumed, so you always know how current the numbers you are reading
actually are.
