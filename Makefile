PYTHON ?= python3.12
MIGRATION_DATABASE_URL ?= postgresql+psycopg://governed_admin:governed_admin_dev@127.0.0.1:5432/governed_analytics
LOADER_DATABASE_URL ?= postgresql+psycopg://analytics_loader:analytics_loader_dev@127.0.0.1:5432/governed_analytics
DATABASE_URL ?= postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics

export MIGRATION_DATABASE_URL
export LOADER_DATABASE_URL
export DATABASE_URL

.PHONY: doctor venv sync lint format typecheck test check db-up db-down migrate migration-check test-integration data-tiny data-full data-verify metrics-check eval-fixture eval-week2-fixture eval-week3-fixture

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

eval-week2-fixture:
	@uv run governed-eval week2 --dataset tiny --mode fixture

eval-week3-fixture:
	@uv run governed-eval week3 --dataset tiny --mode fixture --report-path-file artifacts/evals/week3/fixture-report-path.txt

check: lint typecheck test
