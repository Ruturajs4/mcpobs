PY := .venv/Scripts/python.exe
COMPOSE := docker compose

.PHONY: browser docs docs-build sdk sdk-check help up down demo verify attrs lag logs ps clean test lint typecheck check freshness traces metrics deferred rollups devkeys invite archive up-lite down-lite devkeys-lite up-byo down-byo

help:
	@echo "up        - start clickhouse + kafka + collector + normalizer"
	@echo "down      - stop the stack (keeps volumes)"
	@echo "up-lite       - start the lite (self-host) stack: no kafka/collector/redis/minio"
	@echo "down-lite     - stop the lite stack (keeps volumes)"
	@echo "devkeys-lite  - provision dev org + API keys against the lite stack's Postgres"
	@echo "up-byo        - start ingest+query only, against a database YOU provide (.env required)"
	@echo "down-byo      - stop the bring-your-own-database stack"
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

# Same host ports as the full stack, deliberately (docker-compose.lite.yml) --
# so `down` before `up-lite`, never both at once.
up-lite:
	$(COMPOSE) -f docker-compose.lite.yml up -d --build
	@echo "waiting for lite stack..."
	$(PY) scripts/wait_ready.py --lite

down-lite:
	$(COMPOSE) -f docker-compose.lite.yml down

# CONTROL_PLANE_DSN set explicitly: ControlPlane()'s built-in fallback
# (control/repository.py) points at 5432, but every compose file -- lite
# included -- publishes Postgres on 5433. Not relying on that default here.
devkeys-lite:
	@CONTROL_PLANE_DSN=postgresql://mcpobs:mcpobs@localhost:5433/mcpobs_control $(PY) scripts/admin.py devkeys

# No database containers, so no wait-for-postgres step here -- CONTROL_PLANE_DSN
# and CLICKHOUSE_* must already point at something reachable. Reads a `.env`
# file at the repo root (docker compose's own convention) or exported shell
# vars; docker-compose.byo-db.yml refuses to start with any of them unset.
up-byo:
	$(COMPOSE) -f docker-compose.byo-db.yml up -d --build
	@echo "waiting for ingest + query against your database..."
	$(PY) scripts/wait_ready.py --lite

down-byo:
	$(COMPOSE) -f docker-compose.byo-db.yml down

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

browser:
	@echo "browser flows -- needs 'make up' and 'make devkeys' first"
	$(PY) -m playwright install --with-deps chromium
	$(PY) -m pytest tests/test_browser_flows.py -v

sdk:
	rm -rf dist
	$(PY) -m build
	$(PY) -m twine check dist/*

# Publishing is a TAG, never a make target. PyPI does not allow re-uploading a
# version -- not even a deleted one -- so it must not be one keystroke away.
sdk-check:
	@echo "to publish: git tag sdk-v$$($(PY) -c 'import tomllib;print(tomllib.load(open(\"pyproject.toml\",\"rb\"))[\"project\"][\"version\"])') && git push --tags"
