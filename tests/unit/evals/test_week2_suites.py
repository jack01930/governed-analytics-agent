from __future__ import annotations

from pathlib import Path

import pytest

from governed_analytics.evals.suites import (
    EvaluationCase,
    _validate_oracle_sql,
    load_active_suites,
    load_week2_cases,
    resolved_oracle_path,
)
from governed_analytics.safety.sql_policy import SqlPolicyError, SqlRejectionCode, validate_sql


def test_active_registry_has_exactly_seventy_reviewed_cases_and_expected_distribution() -> None:
    cases = load_active_suites()

    assert len(cases) == 70
    assert {
        suite: sum(case.suite == suite for case in cases)
        for suite in (
            "core-v2",
            "paraphrase",
            "boundary",
            "safety",
        )
    } == {"core-v2": 20, "paraphrase": 20, "boundary": 10, "safety": 20}
    assert all(case.review_status == "approved" and case.risk_tags for case in cases)


def test_core_v2_is_self_contained_and_maps_in_order_to_frozen_core_v1_oracles() -> None:
    cases = load_active_suites()
    core = tuple(case for case in cases if case.suite == "core-v2")

    assert tuple(case.case_id for case in core) == tuple(
        f"C2{number:02d}" for number in range(1, 21)
    )
    assert tuple(case.legacy_case_id for case in core) == tuple(
        f"G{number:03d}" for number in range(1, 21)
    )
    assert all(case.question is not None for case in core)
    assert all(
        "该周" not in (case.question or "") and "上述" not in (case.question or "") for case in core
    )
    assert all(case.oracle_sql_path is not None and case.oracle_sql_path.is_file() for case in core)


def test_paraphrases_resolve_their_core_v2_oracle_without_copying_a_sql_path() -> None:
    cases = load_active_suites()
    by_id = {case.case_id: case for case in cases}
    paraphrases = tuple(case for case in cases if case.suite == "paraphrase")

    assert len(paraphrases) == 20
    for case in paraphrases:
        assert case.oracle_sql_path is None
        assert case.oracle_ref == case.intent_id
        assert resolved_oracle_path(case, cases) == by_id[case.intent_id].oracle_sql_path
        assert "上一周" not in (case.question or "")
        assert "此前" not in (case.question or "")
        assert "六月份" not in (case.question or "")
        assert "2026-" in (case.question or "")


def test_safety_cases_are_direct_guard_inputs_not_model_questions() -> None:
    cases = load_active_suites()
    safety = tuple(case for case in cases if case.suite == "safety")
    sampling = next(case for case in safety if case.case_id == "S215")

    assert len(safety) == 20
    assert all(case.question is None and case.candidate_sql for case in safety)
    assert all(case.oracle_sql_path is None and case.oracle_ref is None for case in safety)
    assert all(case.intent_id == case.case_id for case in safety)
    assert {case.expected_rejection for case in safety} == {
        SqlRejectionCode.NOT_READONLY_QUERY,
        SqlRejectionCode.MULTIPLE_STATEMENTS,
        SqlRejectionCode.FORBIDDEN_RELATION,
        SqlRejectionCode.FORBIDDEN_FUNCTION,
        SqlRejectionCode.NONDETERMINISTIC_FUNCTION,
        SqlRejectionCode.SELECT_STAR,
        SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    }
    assert sampling.risk_tags == ("nondeterministic_sampling",)
    assert "tablesample" in (sampling.candidate_sql or "").lower()
    assert sampling.expected_rejection is SqlRejectionCode.NONDETERMINISTIC_FUNCTION
    for case in safety:
        with pytest.raises(SqlPolicyError) as raised:
            validate_sql(case.candidate_sql or "")
        assert raised.value.code is case.expected_rejection


def test_boundary_cases_have_ten_checked_expected_results_and_resolved_oracles() -> None:
    cases = load_active_suites()
    boundary = tuple(case for case in cases if case.suite == "boundary")

    assert len(boundary) == 10
    assert all(
        case.expected_result_path is not None and case.expected_result_path.is_file()
        for case in boundary
    )
    assert all(resolved_oracle_path(case, cases).is_file() for case in boundary)
    assert all("2026-" in (case.question or "") for case in boundary)


def test_case_model_rejects_mixed_execute_and_safety_contracts() -> None:
    core = next(case for case in load_active_suites() if case.case_id == "C201")
    payload = core.model_dump()
    payload["candidate_sql"] = "select 1"

    with pytest.raises(ValueError, match="cannot declare safety SQL"):
        EvaluationCase.model_validate(payload)


def test_loader_rejects_duplicate_yaml_keys_and_oracle_paths_outside_repository(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("- case_id: C201\n  case_id: C202\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Week 2 evaluation registry YAML"):
        load_week2_cases((duplicate,))

    outside = tmp_path / "outside.sql"
    outside.write_text("select 1", encoding="utf-8")
    registry = tmp_path / "outside-path.yaml"
    registry.write_text(
        f"""
- case_id: C201
  suite: core-v2
  intent_id: C201
  question: A complete question
  category: metric
  difficulty: easy
  risk_tags: [time_window]
  expected_behavior: execute
  source: test
  review_status: approved
  legacy_case_id: G001
  comparison: scalar
  numeric_columns: [gmv]
  oracle_sql_path: {outside}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside repository"):
        load_week2_cases((registry,))


def test_loader_rejects_oracle_reference_that_is_not_a_core_direct_oracle(tmp_path: Path) -> None:
    registry = tmp_path / "dangling-reference.yaml"
    registry.write_text(
        """
- case_id: P201
  suite: paraphrase
  intent_id: C201
  question: A complete paraphrase
  category: metric
  difficulty: easy
  risk_tags: [time_window]
  expected_behavior: execute
  source: test
  review_status: approved
  comparison: scalar
  numeric_columns: [gmv]
  oracle_ref: C201
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="oracle_ref must resolve"):
        load_week2_cases((registry,))


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("-- G001 metric_version=1.0.0\nselect 1 as gmv; select 2 as gmv;\n", "exactly one"),
        ("-- C201 metric_version=1.0.0\nselect 1 as gmv\n", "comment must identify"),
        ("-- G001 metric_version=1.0.0\nselect 1 as gmv, 2 as gmv\n", "duplicate output"),
        (
            "-- G001 metric_version=1.0.0\nselect region as region, 1 as gmv_loss from orders\n",
            "ORDER BY",
        ),
        ("-- G001 metric_version=1.0.0\nselect now() as gmv\n", "nondeterministic"),
    ],
)
def test_oracle_validator_rejects_noncanonical_or_ambiguous_sql(
    tmp_path: Path, sql: str, message: str
) -> None:
    path = tmp_path / "oracle.sql"
    path.write_text(sql, encoding="utf-8")
    payload = {
        "case_id": "C201",
        "suite": "core-v2",
        "intent_id": "C201",
        "question": "A complete question",
        "category": "metric",
        "difficulty": "easy",
        "risk_tags": ["time_window"],
        "expected_behavior": "execute",
        "source": "test",
        "review_status": "approved",
        "legacy_case_id": "G001",
        "comparison": "top_k" if "region" in sql else "scalar",
        "key_columns": ["region"] if "region" in sql else [],
        "numeric_columns": ["gmv_loss"] if "region" in sql else ["gmv"],
        "oracle_sql_path": path,
    }
    case = EvaluationCase.model_validate(payload)

    with pytest.raises(ValueError, match=message):
        _validate_oracle_sql(case, path)
