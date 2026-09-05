from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from governed_analytics.agent.contracts import (
    BehaviorAction,
    BehaviorReasonCode,
    FinalStatus,
    StopReason,
)
from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.week3.models import (
    BehaviorScore,
    BudgetConfiguration,
    BudgetOverrides,
    BudgetScore,
    CandidateScore,
    FrozenExpectedResult,
    ModelIdentifier,
    SuiteScore,
    ToolScore,
    Week3CaseResult,
    Week3RunReport,
    derive_executed_manifest_sha256,
)


def test_candidate_strict_pass_is_not_forgeable() -> None:
    with pytest.raises(ValidationError, match="five-part"):
        CandidateScore(
            result_score=Decimal("1"),
            output_contract_conformant=True,
            answer_contract_validated=False,
            execution_succeeded=True,
            possibly_truncated=False,
            strict_pass=True,
        )


def test_report_models_cannot_represent_sensitive_execution_fields() -> None:
    forbidden = {
        "sql",
        "rows",
        "prompt",
        "oracle_query_id",
        "endpoint",
        "api_key",
        "deadline_monotonic",
        "question",
        "result_summary",
    }

    schemas = (Week3CaseResult.model_json_schema(), Week3RunReport.model_json_schema())
    assert all(not forbidden.intersection(schema.get("properties", {})) for schema in schemas)


def test_expected_result_uses_eval_query_result_and_is_frozen() -> None:
    frozen = FrozenExpectedResult(
        oracle_query_id="a" * 64,
        result=QueryResult(columns=("gmv",), rows=((Decimal("1.20"),),)),
    )

    assert type(frozen.result) is QueryResult
    with pytest.raises(ValidationError):
        frozen.oracle_query_id = "b" * 64


@pytest.mark.parametrize(
    "non_finite",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_expected_result_recursively_rejects_non_finite_numbers(non_finite: object) -> None:
    result = QueryResult(columns=("gmv",), rows=(({"nested": [non_finite]},),))

    with pytest.raises(ValidationError, match="finite"):
        FrozenExpectedResult(oracle_query_id="a" * 64, result=result)


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
        expected_behavior=BehaviorAction.CLARIFY,
        expected_missing_fields=("metric", "time_window"),
        expected_final_status=FinalStatus.CLARIFICATION_REQUIRED,
        expected_stop_reason=StopReason.MISSING_REQUIRED_FIELDS,
        observed_final_status=FinalStatus.CLARIFICATION_REQUIRED,
        observed_stop_reason=StopReason.MISSING_REQUIRED_FIELDS,
        suite_score=SuiteScore(suite="behavior", conformant=True),
        behavior_score=BehaviorScore(
            action_conformant=True,
            reason_conformant=True,
            missing_fields_conformant=True,
            conformant=True,
        ),
        tool_score=ToolScore(
            required_present=True, forbidden_absent=True, sequence_conformant=True, conformant=True
        ),
        budget_score=BudgetScore(
            conformant=True,
            action_loops=0,
            llm_calls=1,
            tool_calls=0,
            execute_calls=0,
            profile_calls=0,
            repair_count=0,
            input_tokens=0,
            output_tokens=0,
            committed_cost_cny=Decimal("0"),
            soft_cap_reached=False,
            max_action_loops=1,
            max_llm_calls=1,
            max_tool_calls=1,
            max_execute_calls=1,
            max_profile_calls=1,
            max_repairs=1,
            expected_repair_count=0,
            hard_cost_cny=Decimal("1"),
        ),
        natural_refusal=True,
        observed_behavior=BehaviorAction.CLARIFY,
        observed_behavior_reason=BehaviorReasonCode.MISSING_METRIC,
        observed_missing_fields=("metric", "time_window"),
        resolved_models=("fixture-agent",),
        model_trace_calls=1,
        model_identity_complete=True,
    )
    overall = "a" * 64
    report = Week3RunReport(
        mode="fixture",
        overall_manifest_sha256=overall,
        known_cohort_sha256="b" * 64,
        heldout_cohort_sha256="c" * 64,
        executed_manifest_sha256=derive_executed_manifest_sha256(
            overall_manifest_sha256=overall, mode="fixture", case_ids=("W3K001",)
        ),
        report_scope="partial_test",
        resolved_models=("fixture-agent",),
        cases=(case,),
    )

    assert report.case_count == report.passed_count == 1
    with pytest.raises(ValidationError):
        Week3CaseResult.model_validate(
            {**case.model_dump(exclude_computed_fields=True), "passed": False}
        )
    for field, forged in (
        (
            "behavior_score",
            BehaviorScore(
                action_conformant=False,
                reason_conformant=False,
                missing_fields_conformant=False,
                conformant=False,
            ),
        ),
        (
            "tool_score",
            ToolScore(
                required_present=False,
                forbidden_absent=True,
                sequence_conformant=True,
                conformant=False,
            ),
        ),
    ):
        with pytest.raises(ValidationError):
            Week3CaseResult.model_validate(
                {**case.model_dump(exclude_computed_fields=True), field: forged}
            )
    assert report.protocol_version == "week3-agent-evaluation-v1"
    assert (
        Week3RunReport.model_validate(
            {
                **report.model_dump(exclude_computed_fields=True),
                "protocol_version": "week3-agent-evaluation-v1",
            }
        ).protocol_version
        == "week3-agent-evaluation-v1"
    )
    with pytest.raises(ValidationError):
        Week3RunReport.model_validate(
            {
                **report.model_dump(exclude_computed_fields=True),
                "protocol_version": "week3-evaluation-v1",
            }
        )
    with pytest.raises(ValidationError):
        Week3RunReport.model_validate({**report.model_dump(), "case_count": 2})
    with pytest.raises(ValidationError, match="executed manifest"):
        Week3RunReport.model_validate(
            {
                **report.model_dump(exclude_computed_fields=True),
                "executed_manifest_sha256": "d" * 64,
            }
        )

    base_case = case.model_dump(exclude_computed_fields=True)
    for update in (
        {
            "expected_behavior": BehaviorAction.REFUSE,
            "expected_missing_fields": (),
            "expected_final_status": FinalStatus.REFUSED,
            "expected_stop_reason": StopReason.SENSITIVE_DATA_REQUEST,
            "observed_behavior": BehaviorAction.REFUSE,
            "observed_behavior_reason": BehaviorReasonCode.SENSITIVE_DATA_REQUEST,
            "observed_missing_fields": (),
        },
        {"model_identity_complete": False},
        {"natural_refusal": False},
    ):
        with pytest.raises(ValidationError):
            Week3CaseResult.model_validate({**base_case, **update})

    configuration = BudgetConfiguration(
        max_action_loops=2,
        max_llm_calls=2,
        max_tool_calls=2,
        max_execute_calls=2,
        max_profile_calls=2,
        max_repairs=1,
        max_concurrent_runs=1,
        timeout_seconds=1,
        soft_cost_cny=Decimal("0.5"),
        hard_cost_cny=Decimal("1"),
    )
    with pytest.raises(ValidationError, match="case budget limits"):
        Week3RunReport.model_validate(
            {
                **report.model_dump(exclude_computed_fields=True),
                "budget_configuration": configuration,
            }
        )

    with pytest.raises(ValidationError, match="executed manifest"):
        Week3RunReport.model_validate(
            {
                **report.model_dump(exclude_computed_fields=True),
                "executed_manifest_sha256": "0" * 64,
            }
        )


def test_budget_case_uses_partial_evidence_terminal_contract() -> None:
    from governed_analytics.evals.week3.suites import load_week3_cases

    case = load_week3_cases()[28]

    assert case.case_id == "W3K029"
    assert case.expected_final_status is FinalStatus.PARTIAL
    assert case.expected_stop_reason is StopReason.EVIDENCE_PARTIAL
    assert case.budget_overrides == BudgetOverrides(max_tool_calls=3)


@pytest.mark.parametrize(
    "model_id",
    ("sk-secret", "pk-secret", "bearer-token", "provider/model", "model id"),
)
def test_report_rejects_credential_shaped_or_noncanonical_model_ids(model_id: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ModelIdentifier).validate_python(model_id)
