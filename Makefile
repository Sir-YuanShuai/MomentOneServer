.PHONY: install dev check format lint type test test-integration db-up db-down db-reset db-logs migrate migrate-new compose-up compose-down compose-logs compose-migrate mcp-apps

PYTHON ?= .venv/bin/python
DOCKER_COMPOSE ?= $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; elif docker-compose version >/dev/null 2>&1; then echo "docker-compose"; else echo "docker compose"; fi)

install:
	$(PYTHON) -m pip install -e '.[dev]'

dev:
	$(PYTHON) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

format:
	$(PYTHON) -m ruff format app tests
	$(PYTHON) -m ruff check --fix app tests

lint:
	$(PYTHON) -m ruff format --check app tests
	$(PYTHON) -m ruff check app tests

type:
	$(PYTHON) -m pyright

test:
	$(PYTHON) -m pytest -m "not integration"

test-integration:
	$(PYTHON) -m pytest -m integration

check: lint type test

db-up:
	$(DOCKER_COMPOSE) up -d postgres

db-down:
	$(DOCKER_COMPOSE) stop postgres

db-reset:
	$(DOCKER_COMPOSE) down --volumes
	$(DOCKER_COMPOSE) up -d postgres

db-logs:
	$(DOCKER_COMPOSE) logs -f postgres

migrate:
	$(PYTHON) -m alembic upgrade head

migrate-new:
	@test -n "$(name)" || (echo 'Usage: make migrate-new name="add moments table"' && exit 1)
	$(PYTHON) -m alembic revision --autogenerate -m "$(name)"

mcp-apps:
	cd mcp_apps/bookkeeping && npm run build

compose-up:
	$(DOCKER_COMPOSE) up -d --build

compose-down:
	$(DOCKER_COMPOSE) down

compose-logs:
	$(DOCKER_COMPOSE) logs -f api postgres

compose-migrate:
	$(DOCKER_COMPOSE) run --rm api alembic upgrade head
