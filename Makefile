PY := .venv/Scripts/python.exe
COMPOSE := docker compose

.PHONY: browser docs docs-build sdk sdk-check help demo verify attrs lag logs ps clean test lint typecheck check freshness traces metrics deferred rollups archive up-lite down-lite

# NO `up`/`down`/`devkeys`/`invite`/`up-byo`/`down-byo`/`devkeys-lite` here:
# those targets ran docker-compose.yml, docker-compose.byo-db.yml and
# scripts/admin.py, all of which are the multi-tenant control plane and live
# in the private ECC repo now (docs/decisions.md D180). `up-lite` is this
# repo's whole self-host story -- single tenant, no keys to provision, no
# admin CLI needed.

help:
	@echo "up-lite       - start the lite (self-host) stack: clickhouse + ingest + query"
	@echo "down-lite     - stop the lite stack (keeps volumes)"
	@echo "demo      - run the MCP tool scenarios over both transports"
	@echo "verify    - run the acceptance assertions against the FULL stack (ECC only -- see below)"
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

up-lite:
	$(COMPOSE) -f docker-compose.lite.yml up -d --build
	@echo "waiting for lite stack..."
	$(PY) scripts/wait_ready.py --lite

down-lite:
	$(COMPOSE) -f docker-compose.lite.yml down

# LocalAuthenticator (control/local.py) accepts any token, so there is
# nothing to provision here -- no keys, no org, no CLI. `make demo` just
# needs ingest reachable.
demo:
	$(PY) -m demo_server.scenarios both

# ECC ONLY: scripts/verify.py drives docker-compose.yml's FULL topology
# (Kafka offsets, stopping/starting the normalizer container, the F-series
# control-plane assertions) -- none of which exists in this repo anymore
# (docs/decisions.md D180). Left wired here for the private ECC repo, which
# has both the compose file and the control plane this script needs.
verify:
	$(PY) scripts/verify.py

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
	@echo "browser flows -- needs 'make up-lite' first"
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
