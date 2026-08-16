# Databases, caches and APIs

Your tool calls a database or a cache; we show it as a child span under the tool
call, with its own timing, so a slow tool says *where* the time went instead of
looking like unexplained server time.

**We do not patch your clients for you.** Adding observability to an MCP server
should not silently monkey-patch your database driver, so this is something you
turn on. There are two ways, and the first is better when you can use it.

=== "OpenTelemetry agent (preferred)"

    Nothing from us involved:

    ```bash
    pip install opentelemetry-instrumentation-redis \
                opentelemetry-instrumentation-psycopg
    opentelemetry-instrument python -m your_server
    ```

    It picks up any instrumentation package the day you install it.

=== "One call, in-process"

    For servers you do not launch yourself — which is most MCP servers, because
    the *client* spawns them over stdio and you do not own that command line:

    ```python
    from mcpobs import instrument, instrument_downstream

    instrument(mcp)
    report = instrument_downstream()
    # {'httpx': 'instrumented', 'redis': 'instrumented', 'psycopg': 'instrumented', ...}
    ```

    It discovers whatever you have installed through OpenTelemetry's own
    entry-point group — the same mechanism the agent uses — so
    `pip install opentelemetry-instrumentation-redis` is the entire integration.

    It returns a **report** rather than nothing, because a call that patches an
    unknown set of libraries should tell you what it touched. It **never
    raises**: one package with a version conflict is reported and skipped, never
    allowed to stop your server booting.

    ```python
    instrument_downstream(exclude=("sqlite3",))   # opt individual ones out
    ```

## What you get per span kind

| Called | Shown as | Fields |
| --- | --- | --- |
| Redis, Postgres, MySQL, Mongo, SQLite | `db` | system, operation, collection/table, statement (**redacted before storage**) |
| HTTP (httpx, requests, aiohttp) | `http` | method, status, host, URL; bodies and headers with `instrument_httpx()` |
| OpenAI, Anthropic, Bedrock | `llm` | system, model, input/output tokens |
| Kafka, RabbitMQ, SQS | `messaging` | stored and rendered generically |

!!! warning "Key-value stores have no table"

    Redis and Memcached spans carry an operation (`GET`, `SETEX`, `DEL`) and no
    collection, because there is nothing to name. The key itself is *not*
    recorded: a Redis key is frequently customer data (`customer:acme:tier`), so
    it is redacted like any other statement argument.

SQL statements are redacted at normalize time, never at render — a secret that
reaches storage cannot be recalled.

## Use an explicit cursor

`opentelemetry-instrumentation-dbapi` wraps `Cursor.execute`, **not** the
`Connection.execute` shortcut. The idiomatic one-liner produces no span at all:

```python
# No span
conn.execute("SELECT 1")

# Span
cur = conn.cursor()
cur.execute("SELECT 1")
```

This is the most common cause of "my database calls do not show up".
