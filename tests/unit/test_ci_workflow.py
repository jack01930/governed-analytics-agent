"""Guard database CI run steps against expensive datasets and common network clients."""

import json
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]


def _database_run_steps(workflow: str) -> list[str]:
    parsed = yaml.safe_load(workflow)
    steps = parsed["jobs"]["database"]["steps"]
    return [step["run"] for step in steps if isinstance(step, dict) and "run" in step]


def _normalized(run_steps: list[str]) -> list[str]:
    return [" ".join(step.lower().split()) for step in run_steps]


def _assert_database_runs_are_safe(workflow: str) -> None:
    normalized = _normalized(_database_run_steps(workflow))
    assert normalized[-4:] == [
        "uv run governed-data generate --scale tiny",
        "uv run governed-data verify --scale tiny",
        "uv run pytest tests/unit/metrics tests/integration/metrics -v",
        "uv run governed-eval baseline --dataset tiny --mode fixture",
    ]
    forbidden = (
        "--scale full",
        "data-full",
        "governed-data generate full",
        "curl",
        "wget",
        "httpie",
        "model_api_key",
        "--mode live",
        "--live",
    )
    assert all(not any(token in step for token in forbidden) for step in normalized)
    assert all("http://" not in step and "https://" not in step for step in normalized)


def test_database_ci_uses_only_local_tiny_data_evidence() -> None:
    _assert_database_runs_are_safe(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "run",
    ("CURL\thttps://example.test", "uv run governed-data generate --scale full", "make data-full"),
)
def test_ci_guard_rejects_case_whitespace_and_full_variants(run: str) -> None:
    workflow = f"""
jobs:
  database:
    steps:
      - run: {json.dumps(run)}
      - run: uv run governed-data generate --scale tiny
      - run: uv run governed-data verify --scale tiny
      - run: uv run pytest tests/unit/metrics tests/integration/metrics -v
"""
    with pytest.raises(AssertionError):
        _assert_database_runs_are_safe(workflow)


def test_ci_guard_only_inspects_database_run_steps_and_allows_uses_urls() -> None:
    workflow = """
jobs:
  quality:
    steps:
      - run: curl https://allowed-outside-database.example
  database:
    steps:
      - uses: https://example.test/action@v1
      - run: uv run governed-data generate --scale tiny
      - run: uv run governed-data verify --scale tiny
      - run: uv run pytest tests/unit/metrics tests/integration/metrics -v
"""
    assert _database_run_steps(workflow) == [
        "uv run governed-data generate --scale tiny",
        "uv run governed-data verify --scale tiny",
        "uv run pytest tests/unit/metrics tests/integration/metrics -v",
    ]
