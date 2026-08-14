"""Typed configuration.

Replaces module-level `os.getenv` calls scattered across five files, which made
the real configuration surface impossible to see in one place and impossible to
type-check.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Kafka
    kafka_bootstrap: str = "localhost:9092"
    kafka_host_bootstrap: str = "localhost:29092"
    kafka_topic: str = "otlp.spans.raw"
    kafka_dlq_topic: str = "otlp.spans.dlq"
    kafka_group_id: str = "normalizer"

    # ClickHouse
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_db: str = "mcpobs"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""

    # Batching. Deliberate defaults: 10k rows is a healthy ClickHouse insert
    # block; 5s bounds ingest freshness, the headline pipeline metric.
    batch_max_rows: int = 10_000
    batch_max_seconds: float = 5.0

    @property
    def clickhouse_dsn(self) -> str:
        return f"http://{self.clickhouse_host}:{self.clickhouse_port}/{self.clickhouse_db}"


settings = Settings()
