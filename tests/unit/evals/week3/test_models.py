from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from governed_analytics.agent.contracts import ActionType
from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.week3.models import (
    BudgetOverrides,
    FrozenExpectedResult,
    Week3CaseResult,
    Week3RunReport,
)


def test_expected_result_uses_eval_query_result_and_is_frozen() -> None:
    frozen = FrozenExpectedResult(
        oracle_query_id="a" * 64,
        result=QueryResult(columns=("gmv",), rows=((Decimal("1.20"),),)),
    )

    assert type(frozen.result) is QueryResult
    with pytest.raises(ValidationError):
        frozen.oracle_query_id = "b" * 64


def test_budget_overrides_require_a_coherent_tool_ceiling() -> None:
    assert BudgetOverrides(max_tool_calls=3, max_execute_calls=2).max_tool_calls == 3
    with pytest.raises(ValidationError):
        BudgetOverrides(max_tool_calls=2, max_execute_calls=3)


def test_week3_report_aggregates_are_derived_from_cases_only() -> None:
    case = Week3CaseResult(
        case_id="W3K001",
        cohort="known",
        suite="behavior",
        passed=True,
        first_candidate_conformant=True,
        final_conformant=True,
        behavior_conformant=True,
        tools_conformant=True,
        evidence_conformant=True,
        budget_conformant=True,
        observed_tools=(ActionType.EXECUTE_SQL,),
    )
    report = Week3RunReport(
        mode="fixture",
        overall_manifest_sha256="a" * 64,
        known_cohort_sha256="b" * 64,
        heldout_cohort_sha256="c" * 64,
        cases=(case,),
    )

    assert report.case_count == report.passed_count == 1
    with pytest.raises(ValidationError):
        Week3RunReport.model_validate({**report.model_dump(), "case_count": 2})
