# Query API

The HTTP API the console is built on. Every endpoint requires a **read**-scoped
API key.

```bash
curl -H "x-api-key: $MCPOBS_READ_KEY" \
     "http://localhost:8080/api/v1/overview?window_minutes=60"
```

## Authentication

Pass the key as `x-api-key`, or as `Authorization: Bearer <key>`.

**Tenancy comes from the key, never from a parameter.** There is no `?tenant=`
override — an endpoint cannot scope a query to the wrong tenant, because an
endpoint never chooses one.

## Endpoints

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/overview` | Fleet health: volume, error rate, breakdown, latency |
| `GET /api/v1/servers` | One row per reporting server |
| `GET /api/v1/capabilities?kind=` | Tools, prompts, resources or protocol methods |
| `GET /api/v1/traces` | Trace list, filtered and paginated |
| `GET /api/v1/traces/{trace_id}` | One trace, ordered, with full span detail |
| `GET /api/v1/errors` | Failing traces only |
| `GET /api/v1/filters?view=` | Available filters and their values |

## Common parameters

| Parameter | Meaning |
| --- | --- |
| `window_minutes` | Time window. Default 60, maximum 7 days |
| `limit` | Page size, maximum 200 |
| `cursor` | Opaque pagination token from `next_cursor` |

Filter parameters are documented per view by `GET /api/v1/filters`.

## Pagination

Trace and error lists use **keyset** pagination, not offset. Follow
`next_cursor` from each response:

```bash
curl -H "x-api-key: $KEY" \
     "http://localhost:8080/api/v1/traces?limit=50&cursor=<next_cursor>"
```

The cursor is opaque on purpose. A readable cursor invites clients to construct
their own, after which the sort key can never change.

!!! note "A trace link never expires with the window"

    `GET /api/v1/traces/{trace_id}` is deliberately not window-scoped. You
    follow a link to a trace, and it would be infuriating for that to 404
    because the default window moved past it.
