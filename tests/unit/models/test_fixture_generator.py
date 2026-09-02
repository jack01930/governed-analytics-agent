from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from governed_analytics.evals.golden import load_golden_cases
from governed_analytics.models.fixtures import FixtureSqlGenerator
from governed_analytics.models.prompts import (
    BASELINE_PROMPT_VERSION,
    BASELINE_SYSTEM_PROMPT_V1,
    build_baseline_user_prompt,
)
from governed_analytics.models.protocols import SqlGenerationRequest, SqlGenerator

FIXTURE_PATH = "evals/fixtures/baseline_sql.json"
EXPECTED_IDS = tuple(f"G{number:03d}" for number in range(1, 21))


def _invalid_full_fixture(g001_value: object) -> str:
    return json.dumps({case_id: "select 1" for case_id in EXPECTED_IDS} | {"G001": g001_value})


def _normalized_sql(query: exp.Expr) -> str:
    normalized = query.copy()
    for node in normalized.walk():
        node.comments = []
    return normalized.sql(dialect="postgres", pretty=False, normalize=True)


def _request(case_id: str = "G001") -> SqlGenerationRequest:
    return SqlGenerationRequest(
        case_id=case_id,
        question="2026-06-08 至 2026-06-14 的 GMV 是多少\uFF1F",
        schema_context="orders(order_id, status, ordered_at); order_items(order_id, net_amount)",
        metric_context="gmv = sum(order_items.net_amount) for valid orders",
    )


@pytest.mark.asyncio
async def test_fixture_generator_returns_case_sql_without_io_after_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = FixtureSqlGenerator.from_path(FIXTURE_PATH)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("unexpected IO"))

    result = await generator.generate(_request())

    assert result.sql.lower().startswith(("select", "with"))
    assert result.provider_model == "fixture-oracle"
    assert result.assumptions == ()
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.latency_ms == 0


def test_request_is_frozen_strict_and_rejects_blank_versioned_fields() -> None:
    request = _request()

    with pytest.raises(ValidationError):
        request.case_id = "G002"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SqlGenerationRequest.model_validate({**request.model_dump(), "unexpected": True})
    for field in ("question", "schema_context", "metric_context"):
        with pytest.raises(ValidationError, match="must not be blank"):
            SqlGenerationRequest.model_validate({**request.model_dump(), field: " \t "})
    with pytest.raises(ValidationError, match="pattern"):
        SqlGenerationRequest.model_validate({**request.model_dump(), "case_id": "G1"})


def test_prompt_is_pinned_and_deterministic() -> None:
    request = _request()

    assert BASELINE_PROMPT_VERSION == "baseline-system-v1"
    assert BASELINE_SYSTEM_PROMPT_V1 == (
        "You generate exactly one PostgreSQL read-only query for the supplied ecommerce question.\n"
        "Use only tables and columns in SCHEMA CONTEXT and metric rules in METRIC CONTEXT.\n"
        "Use half-open UTC time intervals. Do not invent columns or metrics.\n"
        'Return one JSON object with keys "sql" and "assumptions".\n'
        "The SQL must be one SELECT or WITH query. Do not include Markdown fences."
    )
    assert build_baseline_user_prompt(request) == (
        "QUESTION:\n"
        "2026-06-08 至 2026-06-14 的 GMV 是多少\uFF1F\n\n"
        "SCHEMA CONTEXT:\n"
        "orders(order_id, status, ordered_at); order_items(order_id, net_amount)\n\n"
        "METRIC CONTEXT:\n"
        "gmv = sum(order_items.net_amount) for valid orders\n\n"
        'OUTPUT FORMAT:\nReturn only one JSON object with exactly the keys "sql" and "assumptions".'
    )
    assert build_baseline_user_prompt(request) == build_baseline_user_prompt(request)


def test_fixture_has_exact_coverage_and_normalized_ast_equality_to_oracles() -> None:
    fixture = json.loads(Path(FIXTURE_PATH).read_text(encoding="utf-8"))
    assert tuple(fixture) == EXPECTED_IDS

    for case in load_golden_cases("evals/datasets/golden/cases.yaml"):
        fixture_sql = fixture[case.case_id]
        oracle_sql = case.oracle_sql_path.read_text(encoding="utf-8")
        assert fixture_sql.lower().startswith(("select", "with"))
        fixture_ast = sqlglot.parse_one(fixture_sql, read="postgres")
        oracle_ast = sqlglot.parse_one(oracle_sql, read="postgres")
        assert _normalized_sql(fixture_ast) == _normalized_sql(oracle_ast)


def test_fixture_generator_satisfies_sql_generator_protocol() -> None:
    generator: SqlGenerator = FixtureSqlGenerator.from_path(FIXTURE_PATH)
    assert generator is not None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"G001": "select 1", "G001": "select 2"}', "duplicate JSON key"),
        ('{"G001": {"sql": "select 1", "sql": "select 2"}}', "duplicate JSON key"),
        ("{}", "case IDs"),
        ('{"G001": "select 1"}', "case IDs"),
        (
            json.dumps(
                {**{case_id: "select 1" for case_id in EXPECTED_IDS}, "G021": "select 1"}
            ),
            "case IDs",
        ),
        (_invalid_full_fixture(""), "invalid SQL"),
        (_invalid_full_fixture(1), "invalid SQL"),
        (_invalid_full_fixture("select 1; select 2"), "invalid SQL"),
        (_invalid_full_fixture("delete from orders"), "invalid SQL"),
        ("{", "malformed JSON"),
    ],
)
def test_fixture_loader_rejects_invalid_contracts_without_echoing_contents(
    tmp_path: Path, content: str, message: str
) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message) as error:
        FixtureSqlGenerator.from_path(fixture_path)

    assert "select 2" not in str(error.value)


def test_fixture_loader_is_cwd_independent_and_rejects_oracle_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    generator = FixtureSqlGenerator.from_path(FIXTURE_PATH)
    assert generator.case_ids == EXPECTED_IDS

    fixture = json.loads((Path(__file__).resolve().parents[3] / FIXTURE_PATH).read_text("utf-8"))
    fixture["G001"] = "select 1 as gmv"
    fixture_path = tmp_path / "drift.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="Oracle equivalence"):
        FixtureSqlGenerator.from_path(fixture_path)


@pytest.mark.asyncio
async def test_fixture_generator_has_stable_missing_case_error_when_constructed_internally(
) -> None:
    generator = FixtureSqlGenerator._from_sql_by_case_id({"G001": "select 1 as value"})

    with pytest.raises(ValueError, match="fixture SQL unavailable"):
        await generator.generate(_request("G020"))
