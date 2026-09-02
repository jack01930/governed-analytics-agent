PYTHON ?= python3.12

.PHONY: doctor venv sync lint format typecheck test check db-up db-down migrate migration-check test-integration data-tiny data-full data-verify metrics-check eval-fixture

doctor:
	@bash scripts/check_environment.sh

venv:
	@$(PYTHON) -m venv .venv
	@.venv/bin/python --version

sync:
	@uv sync --all-groups

lint:
	@uv run ruff check .

format:
	@uv run ruff format .

typecheck:
	@uv run mypy

test:
	@uv run pytest tests/unit

db-up:
	@docker compose up -d --wait db

db-down:
	@docker compose stop db

migrate:
	@uv run alembic upgrade head

migration-check:
	@uv run alembic current
	@uv run pytest tests/integration/persistence/test_migrations.py -q

test-integration:
	@uv run pytest tests/integration -v

data-tiny:
	@uv run governed-data generate --scale tiny

data-full:
	@uv run governed-data generate --scale full

data-verify:
	@uv run governed-data verify --scale tiny

metrics-check:
	@uv run pytest tests/unit/metrics tests/integration/metrics -v

eval-fixture:
	@uv run governed-eval baseline --dataset tiny --mode fixture

check: lint typecheck test
