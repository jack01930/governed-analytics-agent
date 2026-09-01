PYTHON ?= python3.12

.PHONY: doctor venv sync lint format typecheck test check

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
	@uv run pytest

check: lint typecheck test
