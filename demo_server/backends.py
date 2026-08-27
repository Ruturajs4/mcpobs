"""Real backends for the demo MCP server.

The demo used to reach exactly two things: an in-process SQLite file and an
in-process HTTP server. Both are honest, and both are also the easiest possible
case -- same process, no network, no auth, no connection pool. A trace over them
cannot show what a customer's trace looks like, because a customer's tool calls
Postgres over a socket, checks Redis, hits a partner API that sometimes 500s,
and reads a legacy MySQL table nobody has migrated yet.

So this module gives the demo server FOUR real dependencies:

    Postgres  (5433)  -- the app's own store, separate from the control plane
    MySQL     (3307)  -- a second engine, so `db.system` is visibly not one value
    Redis     (6379)  -- cache reads and writes, the most common non-SQL hop
    HTTP      (local) -- three mock partner APIs on separate ports

NOTHING HERE CALLS THE INTERNET. The "partner APIs" are local servers on
127.0.0.1 that imitate the shapes worth seeing in a trace -- a fast one, a slow
one, and one that fails a fraction of the time. A demo that depended on a third
party would break when that third party was down, and would send data somewhere
we do not control.

Every connection is opened per call rather than pooled. A pool would be more
realistic for a production server and much worse for a demo: pooled connections
hide the connect cost that makes downstream spans interesting, and a pool shared
across the async tools would serialise them.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

log = logging.getLogger("demo.backends")

PG_DSN = os.getenv(
    "DEMO_PG_DSN", "postgresql://mcpobs:mcpobs@127.0.0.1:5433/mcpobs_control"
)
MYSQL = {
    "host": os.getenv("DEMO_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("DEMO_MYSQL_PORT", "3307")),
    "user": os.getenv("DEMO_MYSQL_USER", "mcpobs"),
    "password": os.getenv("DEMO_MYSQL_PASSWORD", "mcpobs"),
    "database": os.getenv("DEMO_MYSQL_DB", "shop"),
}
REDIS_URL = os.getenv("DEMO_REDIS_URL", "redis://127.0.0.1:6379/3")

#: Three partner APIs rather than one. A single downstream host makes every HTTP
#: span look alike; three make `http.host` worth reading in the waterfall.
PARTNER_PORTS = {
    "payments": int(os.getenv("DEMO_PAYMENTS_PORT", "8801")),
    "inventory": int(os.getenv("DEMO_INVENTORY_PORT", "8802")),
    "shipping": int(os.getenv("DEMO_SHIPPING_PORT", "8803")),
}
PARTNERS = {name: f"http://127.0.0.1:{port}" for name, port in PARTNER_PORTS.items()}


# --------------------------------------------------------------------- SQL
def pg_connect() -> Any:
    import psycopg

    return psycopg.connect(PG_DSN, connect_timeout=5)


def mysql_connect() -> Any:
    import pymysql

    return pymysql.connect(connect_timeout=5, **MYSQL)


def redis_client() -> Any:
    import redis

    return redis.Redis.from_url(REDIS_URL, socket_timeout=5)


#: The demo's own tables live in their own schema so they cannot be confused
#: with -- or accidentally drop -- the control plane sharing this Postgres.
PG_SCHEMA = "demo_app"

_PG_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA};
CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.customers (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    tier        TEXT NOT NULL DEFAULT 'standard',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.orders (
    id          SERIAL PRIMARY KEY,
    customer    TEXT NOT NULL,
    sku         TEXT NOT NULL,
    quantity    INT  NOT NULL DEFAULT 1,
    total_cents INT  NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'placed',
    placed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON {PG_SCHEMA}.orders (customer);
"""

_MYSQL_DDL = (
    """CREATE TABLE IF NOT EXISTS inventory (
        sku       VARCHAR(64) PRIMARY KEY,
        warehouse VARCHAR(32) NOT NULL,
        on_hand   INT NOT NULL DEFAULT 0,
        reserved  INT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS shipments (
        id       INT AUTO_INCREMENT PRIMARY KEY,
        sku      VARCHAR(64) NOT NULL,
        carrier  VARCHAR(32) NOT NULL,
        status   VARCHAR(32) NOT NULL DEFAULT 'pending',
        shipped  TIMESTAMP NULL DEFAULT NULL
    )""",
)

CUSTOMERS = [("acme", "enterprise"), ("globex", "standard"), ("initech", "standard"),
             ("umbrella", "enterprise"), ("hooli", "trial")]
SKUS = ["widget-1", "widget-2", "gizmo-9", "sprocket-3", "cog-7"]


def seed() -> dict[str, str]:
    """Create and populate every backend. Idempotent.

    Returns what happened per backend rather than raising, so one unreachable
    dependency does not stop the demo from exercising the other three -- which
    is also how the tools behave, and the reason a partial outage is visible in
    the console rather than fatal.
    """
    status: dict[str, str] = {}

    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(_PG_DDL)
            for name, tier in CUSTOMERS:
                cur.execute(
                    f"INSERT INTO {PG_SCHEMA}.customers (name, tier) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET tier = EXCLUDED.tier",
                    (name, tier),
                )
            cur.execute(f"SELECT count(*) FROM {PG_SCHEMA}.orders")
            existing = cur.fetchone()[0]
            if existing < 40:
                for _ in range(60):
                    cur.execute(
                        f"INSERT INTO {PG_SCHEMA}.orders "
                        "(customer, sku, quantity, total_cents, status) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            random.choice(CUSTOMERS)[0],
                            random.choice(SKUS),
                            random.randint(1, 5),
                            random.randint(500, 25_000),
                            random.choice(["placed", "shipped", "refunded"]),
                        ),
                    )
            conn.commit()
        status["postgres"] = "ready"
    except Exception as exc:
        status["postgres"] = f"unavailable: {exc}"

    try:
        conn = mysql_connect()
        with conn.cursor() as cur:
            for ddl in _MYSQL_DDL:
                cur.execute(ddl)
            for sku in SKUS:
                cur.execute(
                    "INSERT INTO inventory (sku, warehouse, on_hand, reserved) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE on_hand = VALUES(on_hand)",
                    (sku, random.choice(["ams-1", "sfo-2"]), random.randint(0, 400),
                     random.randint(0, 20)),
                )
        conn.commit()
        conn.close()
        status["mysql"] = "ready"
    except Exception as exc:
        status["mysql"] = f"unavailable: {exc}"

    try:
        client = redis_client()
        client.ping()
        for name, tier in CUSTOMERS:
            client.setex(f"customer:{name}:tier", 3600, tier)
        status["redis"] = "ready"
    except Exception as exc:
        status["redis"] = f"unavailable: {exc}"

    return status


# ------------------------------------------------------------- partner APIs
class _Partner(BaseHTTPRequestHandler):
    """One mock partner API. Behaviour comes from the subclass attributes.

    Deliberately imperfect: `fail_rate` and `slow_ms` exist so the traces show
    a downstream that is sometimes slow and sometimes broken. A demo where every
    dependency answers in 2ms teaches nothing about reading a waterfall.
    """

    service = "partner"
    slow_ms = 0
    fail_rate = 0.0

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        self._respond()

    def _respond(self) -> None:
        if self.slow_ms:
            time.sleep(random.uniform(self.slow_ms * 0.4, self.slow_ms) / 1000)
        if random.random() < self.fail_rate:
            body = json.dumps({"error": "upstream_unavailable", "service": self.service})
            self.send_response(503)
        else:
            body = json.dumps({
                "service": self.service,
                "path": self.path,
                "reference": f"{self.service}-{random.randint(10_000, 99_999)}",
                "ok": True,
            })
            self.send_response(200)
        payload = body.encode()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Silenced: this server shares stdio with the MCP JSON-RPC channel."""


class _Payments(_Partner):
    service = "payments"
    slow_ms = 120
    #: Payments is the flaky one. Something in a demo has to fail for the error
    #: taxonomy to have anything to classify.
    fail_rate = 0.25


class _Inventory(_Partner):
    service = "inventory"
    slow_ms = 25


class _Shipping(_Partner):
    service = "shipping"
    slow_ms = 400  # the slow dependency, so one span visibly dominates a trace


_SERVERS: list[HTTPServer] = []


def start_partners() -> dict[str, str]:
    """Start the three mock APIs on background threads. Idempotent."""
    if _SERVERS:
        return PARTNERS
    for handler, port in (
        (_Payments, PARTNER_PORTS["payments"]),
        (_Inventory, PARTNER_PORTS["inventory"]),
        (_Shipping, PARTNER_PORTS["shipping"]),
    ):
        server = HTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _SERVERS.append(server)
    return PARTNERS
