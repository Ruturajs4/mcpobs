"""Block until the local stack is actually usable."""

from __future__ import annotations

import socket
import sys
import time
import urllib.request

CHECKS = [
    ("clickhouse", "http", "http://localhost:8123/ping"),
    ("collector", "http", "http://localhost:13133/"),
    ("kafka", "tcp", ("localhost", 9092)),
]

#: No collector, no kafka -- neither container exists in the lite stack.
#: `ingest`'s own /ready (ingest/app.py) checks ClickHouse itself in lite mode,
#: so it stands in for both the storage and the intake-path checks at once.
LITE_CHECKS = [
    ("clickhouse", "http", "http://localhost:8123/ping"),
    ("ingest", "http", "http://localhost:4319/ready"),
    ("query", "http", "http://localhost:8080/health"),
]


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status < 500
    except Exception:
        return False


def tcp_ok(hostport: tuple[str, int]) -> bool:
    with socket.socket() as sock:
        sock.settimeout(2)
        return sock.connect_ex(hostport) == 0


def main() -> int:
    checks = LITE_CHECKS if "--lite" in sys.argv else CHECKS
    deadline = time.time() + 180
    pending = {name: (kind, target) for name, kind, target in checks}
    while pending and time.time() < deadline:
        for name, (kind, target) in list(pending.items()):
            ok = http_ok(target) if kind == "http" else tcp_ok(target)
            if ok:
                print(f"  ready: {name}")
                pending.pop(name)
        if pending:
            time.sleep(3)
    if pending:
        print(f"NOT READY: {', '.join(pending)}", file=sys.stderr)
        return 1
    print("stack ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
