from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    AnalysisAction,
    AnswerContract,
    BehaviorAction,
    BehaviorDecision,
    BehaviorReasonCode,
    ColumnContract,
    GovernanceSnapshot,
    JsonValue,
    ModelUsage,
    Observation,
    ObservationContract,
    ObservationValidation,
    ResultShape,
    StructuredModelRequest,
    StructuredModelResult,
    TimeWindow,
)


def test_behavior_decision_requires_missing_fields_only_for_clarify() -> None:
    clarify = BehaviorDecision(
        action=BehaviorAction.CLARIFY,
        reason_code=BehaviorReasonCode.MISSING_TIME_WINDOW,
        missing_fields=("time_window",),
        user_message="请补充查询时间范围。",
    )
    assert clarify.missing_fields == ("time_window",)

    with pytest.raises(ValidationError):
        BehaviorDecision(
            action=BehaviorAction.EXECUTE,
            reason_code=BehaviorReasonCode.READY,
            missing_fields=("time_window",),
            user_message="ready",
        )


def test_answer_contract_has_distinct_subcontracts_for_attribution() -> None:
    comparison = ObservationContract(
        contract_id="gmv_comparison",
        hypothesis_id="confirm_decline",
        columns=(ColumnContract(name="current_gmv", data_type="decimal", role="metric"),),
        shape=ResultShape.SCALAR,
        min_rows=1,
        max_rows=1,
    )
    contract = AnswerContract(
        answer_contract_id="gmv_attribution",
        required_hypotheses=("confirm_decline",),
        observation_contracts=(comparison,),
    )

    assert contract.contract("gmv_comparison") is comparison
    with pytest.raises(ValidationError):
        AnswerContract(
            answer_contract_id="bad",
            required_hypotheses=("confirm_decline",),
            observation_contracts=(comparison, comparison),
        )


def test_action_cannot_request_finish_or_omit_execute_contract() -> None:
    with pytest.raises(ValidationError):
        AgentAction(
            action_type=ActionType.EXECUTE_SQL,
            purpose="calculate metric",
            arguments={"sql": "select 1"},
            hypothesis_id="metric_value",
            expected_evidence="metric value",
        )


def test_time_window_is_timezone_aware_and_half_open() -> None:
    with pytest.raises(ValidationError):
        TimeWindow(
            label="current",
            start_at=datetime(2026, 6, 1),
            end_at=datetime(2026, 7, 1, tzinfo=UTC),
        )


def test_analysis_action_rejects_context_tools_and_is_an_agent_action() -> None:
    action = AnalysisAction(
        action_type=ActionType.EXECUTE_SQL,
        purpose="calculate metric",
        arguments={"sql": "select 1"},
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        expected_evidence="metric value",
    )

    assert isinstance(action, AgentAction)
    with pytest.raises(ValidationError):
        AnalysisAction(
            action_type="metric_lookup",  # type: ignore[arg-type]
            purpose="lookup metric",
            arguments={},
            expected_evidence="metric definition",
        )


def test_observation_validation_is_linked_by_id_without_copying_payload() -> None:
    validation = ObservationValidation(
        observation_id="observation-1",
        contract_id="metric_value_contract",
        valid=False,
        error_code="missing_column",
        repairable=True,
    )

    assert validation.observation_id == "observation-1"
    assert "payload" not in type(validation).model_fields


def test_structured_result_tokens_are_read_only_usage_properties() -> None:
    result = StructuredModelResult[BehaviorDecision](
        output=BehaviorDecision(
            action=BehaviorAction.EXECUTE,
            reason_code=BehaviorReasonCode.READY,
            user_message="ready",
        ),
        provider_model="fixture-model",
        usage=ModelUsage(input_tokens=11, output_tokens=7),
        latency_ms=3,
    )

    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert "input_tokens" not in type(result).model_fields
    with pytest.raises(ValidationError):
        StructuredModelResult[BehaviorDecision](
            output=result.output,
            provider_model="fixture-model",
            usage=result.usage,
            latency_ms=3,
            input_tokens=100,  # type: ignore[call-arg]
        )


def test_json_inputs_are_deeply_frozen_and_serialized_deterministically() -> None:
    action = AgentAction(
        action_type=ActionType.EXECUTE_SQL,
        purpose="calculate metric",
        arguments={"query": {"dimensions": ["region"]}},  # type: ignore[dict-item]
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        expected_evidence="metric value",
    )
    first = StructuredModelRequest(
        purpose="behavior",
        system_prompt="Return a decision.",
        user_payload={
            "z": [  # type: ignore[dict-item]
                1,
                {"b": 2, "a": datetime(2026, 6, 1, tzinfo=UTC)},
            ]
        },
        output_schema_name="BehaviorDecision",
        output_schema_summary=BehaviorDecision.model_json_schema(),
        max_output_tokens=128,
    )
    second = StructuredModelRequest(
        purpose="behavior",
        system_prompt="Return a decision.",
        user_payload={"z": (1, {"a": "2026-06-01T00:00:00Z", "b": 2})},
        output_schema_name="BehaviorDecision",
        output_schema_summary=BehaviorDecision.model_json_schema(),
        max_output_tokens=128,
    )

    action_query = cast(Mapping[str, JsonValue], action.arguments["query"])
    with pytest.raises(TypeError):
        action_query["new"] = 1  # type: ignore[index]

    nested = first.user_payload["z"]
    assert isinstance(nested, tuple)
    with pytest.raises(TypeError):
        first.user_payload["new"] = 1  # type: ignore[index]
    nested_mapping = cast(Mapping[str, JsonValue], nested[1])
    with pytest.raises(TypeError):
        nested_mapping["new"] = 1  # type: ignore[index]
    assert first.user_json() == second.user_json()
    assert first.prompt_bytes() == second.prompt_bytes()


def test_structured_request_repair_preserves_bound_schema() -> None:
    request = StructuredModelRequest.for_output(
        purpose="behavior",
        system_prompt="Return a decision.",
        user_payload={"query": "GMV"},
        output_type=BehaviorDecision,
        max_output_tokens=128,
    )

    repair = request.for_repair(failure_category="schema_validation")

    assert request.output_schema_name == "BehaviorDecision"
    assert repair.purpose == "repair"
    assert repair.output_schema_name == request.output_schema_name
    assert repair.output_schema_summary == request.output_schema_summary
    assert repair.user_payload["failure_category"] == "schema_validation"


def test_counters_and_money_are_non_negative() -> None:
    with pytest.raises(ValidationError):
        GovernanceSnapshot(llm_calls=-1)
    with pytest.raises(ValidationError):
        GovernanceSnapshot(committed_cost_cny=Decimal("-0.01"))


def test_observation_shapes_and_safe_summary_exclude_payload() -> None:
    observation = Observation(
        observation_id="observation-1",
        tool_name=ActionType.EXECUTE_SQL,
        purpose="calculate metric",
        ok=True,
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        query_id="a" * 64,
        columns=("gmv",),
        row_count=1,
        payload={"rows": [[Decimal("1.25")]]},  # type: ignore[dict-item]
    )

    assert observation.payload is not None
    assert "payload" not in observation.safe_summary
    assert "rows" not in observation.safe_summary

    with pytest.raises(ValidationError):
        Observation(
            observation_id="observation-2",
            tool_name=ActionType.EXECUTE_SQL,
            purpose="calculate metric",
            ok=False,
            safe_error="query failed",
            payload={"rows": []},  # type: ignore[dict-item]
        )


def test_observation_validation_state_combinations_are_strict() -> None:
    with pytest.raises(ValidationError):
        ObservationValidation(
            observation_id="observation-1",
            contract_id="metric_value_contract",
            valid=True,
            error_code="missing_column",
        )
    with pytest.raises(ValidationError):
        ObservationValidation(
            observation_id="observation-1",
            contract_id="metric_value_contract",
            valid=False,
        )
