# Quotas

Ingest is metered per organisation, per window.

## What happens at the limit

Requests over quota are rejected with **429** and these headers:

| Header | Meaning |
| --- | --- |
| `Retry-After` | Seconds until the window resets |
| `X-Quota-Limit` | Spans allowed in the window |
| `X-Quota-Remaining` | Spans left |
| `X-Quota-Reset` | When the window resets |

Your OTel exporter will retry; spans buffered during a short overage are not
lost unless the buffer fills.

## Soft limits

Before the hard limit, spans are accepted and **marked**. The telemetry keeps
flowing and the overage is visible, rather than data disappearing at a
threshold you did not know you were near.

## Unlimited plans

An unlimited plan short-circuits before the counter is touched at all — there is
no quota bookkeeping on the hot path for organisations that do not have one.

The [self-hosted lite image](../get-started/lite.md) always runs on this
unlimited plan: there's no control plane there to hold a per-org limit
against, so nothing is ever metered or rejected. Everything else on this
page applies to a managed, multi-tenant deployment.
