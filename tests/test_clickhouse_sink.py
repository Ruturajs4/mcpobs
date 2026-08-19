"""ClickHouseSink's client construction: TLS pass-through for BYO-database.

Every compose-managed ClickHouse this project ships (full and lite) talks
plain HTTP on the docker network, so `secure` has been `False` since this
project began and nothing exercised the alternative. Bring-your-own-database
mode (docker-compose.byo-db.yml) is the first deployment that needs it --
ClickHouse Cloud is TLS-only, and `clickhouse_connect.get_client()` defaults
`secure` to `False` when the caller does not pass it explicitly.
"""

from __future__ import annotations

from normalizer.clickhouse_sink import ClickHouseSink
from normalizer.config import Settings


class TestSecurePassthrough:
    def test_secure_flag_reaches_get_client(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_get_client(**kwargs: object):
            captured.update(kwargs)
            return object()

        import normalizer.clickhouse_sink as sink_module

        monkeypatch.setattr(sink_module.clickhouse_connect, "get_client", fake_get_client)

        settings = Settings(clickhouse_secure=True, clickhouse_host="cloud.example.com")
        sink = ClickHouseSink(settings)
        _ = sink.client  # triggers lazy construction

        assert captured["secure"] is True
        assert captured["host"] == "cloud.example.com"

    def test_secure_defaults_false_for_compose_managed_clickhouse(self, monkeypatch) -> None:
        """The property this project has relied on since Day 1, made explicit
        now that a `secure=True` path exists to accidentally default to."""
        captured: dict[str, object] = {}

        def fake_get_client(**kwargs: object):
            captured.update(kwargs)
            return object()

        import normalizer.clickhouse_sink as sink_module

        monkeypatch.setattr(sink_module.clickhouse_connect, "get_client", fake_get_client)

        sink = ClickHouseSink(Settings())
        _ = sink.client

        assert captured["secure"] is False
