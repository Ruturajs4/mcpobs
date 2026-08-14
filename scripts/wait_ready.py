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
    deadline = time.time() + 180
    pending = {name: (kind, target) for name, kind, target in CHECKS}
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
