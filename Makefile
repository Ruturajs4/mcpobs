PY := .venv/Scripts/python.exe
COMPOSE := docker compose

.PHONY: help up down demo verify attrs lag logs ps clean test lint typecheck check

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
	@echo "clean     - down + remove volumes"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check normalizer/ mcpobs/ tests/ scripts/ demo_server/

typecheck:
	$(PY) -m mypy normalizer/ mcpobs/

check: test lint typecheck

up:
	$(COMPOSE) up -d --build
	@echo "waiting for stack..."
	$(PY) scripts/wait_ready.py

down:
	$(COMPOSE) down

demo:
	$(PY) -m demo_server.scenarios both

verify:
	$(PY) scripts/verify_day1.py

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
