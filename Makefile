PY := .venv/Scripts/python.exe
COMPOSE := docker compose

.PHONY: docs docs-build help up down demo verify attrs lag logs ps clean test lint typecheck check freshness traces metrics deferred rollups devkeys invite archive

help:
	@echo "up        - start clickhouse + kafka + collector + normalizer"
	@echo "down      - stop the stack (keeps volumes)"
	@echo "demo      - run the MCP tool scenarios over both transports"
	@echo "verify    - run the A1-A9 acceptance assertions (needs the stack)"
	@echo "test      - unit tests (no stack required)"
	@echo "lint      - ruff"
	@echo "typecheck - mypy over normalizer/"
	@echo "check     - test + lint + typecheck"
	@echo "attrs     - regenerate docs/observed_attributes.md (T3)"
	@echo "lag       - show normalizer consumer group lag"
	@echo "logs      - tail all service logs"
	@echo "freshness - end-to-end pipeline freshness (the headline metric)"
	@echo "traces    - assembled trace summaries"
	@echo "metrics   - the pipeline's own operational metrics (V2 19)"
	@echo "devkeys   - ensure local dev org + API keys (.mcpobs-keys.env)"
	@echo "invite    - ORG=acme EMAIL=x@y.com invite a user (invite-only)"
	@echo "archive   - list what the archiver has written to S3"
	@echo "rollups   - rebuild tool_metrics_1m from spans_raw (run after a replay)"
	@echo "deferred  - open deferral register (what we postponed and why)"
	@echo "clean     - down + remove volumes"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check normalizer/ mcpobs/ query/ control/ ingest/ archiver/ tests/ scripts/ demo_server/

typecheck:
	$(PY) -m mypy normalizer/ mcpobs/ query/ control/ ingest/ archiver/

check: test lint typecheck

up:
	$(COMPOSE) up -d --build
	@echo "waiting for stack..."
	$(PY) scripts/wait_ready.py

down:
	$(COMPOSE) down

# Both targets provision their own keys first. Ingest is authenticated now,
# so a demo run without a key is a wall of 401s -- and `devkeys` is
# idempotent, so calling it every time costs one authentication.
demo: devkeys
	$(PY) -m demo_server.scenarios both

verify: devkeys
	$(PY) scripts/verify.py

devkeys:
	@$(PY) scripts/admin.py devkeys

# Invite someone into an org. There is no self-service signup: an account
# exists only because somebody already inside issued one of these.
invite:
	@$(PY) scripts/admin.py invite --org $(ORG) --email $(EMAIL)

attrs:
	$(PY) scripts/dump_observed_attrs.py

lag:
	docker exec mcpobs-kafka /opt/kafka/bin/kafka-consumer-groups.sh \
		--bootstrap-server localhost:9092 --describe --group normalizer

logs:
	$(COMPOSE) logs -f --tail=80

ps:
	$(COMPOSE) ps

clean:
	$(COMPOSE) down -v

freshness:
	@docker exec mcpobs-clickhouse clickhouse-client -q "SELECT \
	  quantile(0.50)(dateDiff('millisecond', timestamp, ingested_at)) AS p50_ms, \
	  quantile(0.95)(dateDiff('millisecond', timestamp, ingested_at)) AS p95_ms, \
	  max(ingested_at) AS newest, count() AS spans \
	  FROM mcpobs.spans_raw WHERE timestamp > now() - INTERVAL 30 MINUTE FORMAT Vertical"

traces:
	@docker exec mcpobs-clickhouse clickhouse-client -q "SELECT \
	  max(tool_name) AS tool, sum(span_count) AS spans, sum(error_span_count) AS errors, \
	  argMaxMerge(failure_category) AS category, \
	  dateDiff('millisecond', min(start_time), max(end_time)) AS duration_ms \
	  FROM mcpobs.trace_summaries GROUP BY tenant_id, project_id, trace_id \
	  ORDER BY spans DESC, category LIMIT 15"

metrics:
	$(PY) scripts/metrics.py

archive:
	$(PY) scripts/archive_ls.py

rollups:
	$(PY) scripts/recompute_rollups.py

deferred:
	$(PY) scripts/deferred.py

docs:
	$(PY) -m mkdocs serve

docs-build:
	$(PY) -m mkdocs build --strict
