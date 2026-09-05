from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from governed_analytics.agent.contracts import ActionType, FinalStatus, StopReason
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
    assert BudgetOverrides(max_tool_calls=3) == BudgetOverrides(
        max_tool_calls=3,
        max_execute_calls=None,
    )
    assert BudgetOverrides(max_execute_calls=5).max_tool_calls is None
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
    assert report.protocol_version == "week3-agent-evaluation-v1"
    assert Week3RunReport.model_validate(
        {
            **report.model_dump(exclude_computed_fields=True),
            "protocol_version": "week3-agent-evaluation-v1",
        }
    ).protocol_version == "week3-agent-evaluation-v1"
    with pytest.raises(ValidationError):
        Week3RunReport.model_validate(
            {
                **report.model_dump(exclude_computed_fields=True),
                "protocol_version": "week3-evaluation-v1",
            }
        )
    with pytest.raises(ValidationError):
        Week3RunReport.model_validate({**report.model_dump(), "case_count": 2})


def test_budget_case_uses_partial_evidence_terminal_contract() -> None:
    from governed_analytics.evals.week3.suites import load_week3_cases

    case = load_week3_cases()[28]

    assert case.case_id == "W3K029"
    assert case.expected_final_status is FinalStatus.PARTIAL
    assert case.expected_stop_reason is StopReason.EVIDENCE_PARTIAL
    assert case.budget_overrides == BudgetOverrides(max_tool_calls=3)
