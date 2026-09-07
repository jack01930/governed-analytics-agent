import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    AgentModelErrorCategory,
    AnalysisAction,
    AnswerContract,
    BehaviorAction,
    BehaviorDecision,
    BehaviorReasonCode,
    ColumnContract,
    ContextBundle,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    ModelUsage,
    Observation,
    ObservationContract,
    ObservationValidation,
    ResultShape,
    StopReason,
    StructuredModelRequest,
    StructuredModelResult,
    TimeWindow,
    ToolCallTrace,
    ToolInvocation,
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
        columns=(
            ColumnContract(name="current_gmv", data_type="decimal", role="metric", unit="cny"),
        ),
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


def test_answer_contract_covers_every_required_hypothesis() -> None:
    comparison = ObservationContract(
        contract_id="gmv_comparison",
        hypothesis_id="confirm_decline",
        columns=(
            ColumnContract(name="current_gmv", data_type="decimal", role="metric", unit="cny"),
        ),
        shape=ResultShape.SCALAR,
        min_rows=1,
        max_rows=1,
    )

    with pytest.raises(ValidationError):
        AnswerContract(
            answer_contract_id="gmv_attribution",
            required_hypotheses=("confirm_decline", "channel_contribution"),
            observation_contracts=(comparison,),
        )


def test_numeric_metric_columns_require_units_and_other_columns_forbid_them() -> None:
    with pytest.raises(ValidationError, match="numeric metric columns require unit"):
        ColumnContract(name="amount", data_type="decimal", role="metric")
    with pytest.raises(ValidationError, match="only numeric metric columns may carry unit"):
        ColumnContract(name="region", data_type="string", role="dimension", unit="cny")

    column = ColumnContract(name="orders", data_type="integer", role="metric", unit="count")

    assert column.unit == "count"


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
        validation_fingerprint="f" * 64,
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


def test_prompt_bytes_include_provider_envelope_schema_and_fixed_protocol_overhead() -> None:
    request = StructuredModelRequest(
        purpose="behavior",
        system_prompt="Return a decision.",
        user_payload={"query": "六月GMV"},
        output_schema_name="BehaviorDecision",
        output_schema_summary={"title": "BehaviorDecision", "type": "object"},
        max_output_tokens=128,
    )
    expected_envelope = {
        "messages": [
            {"role": "system", "content": request.provider_system_prompt()},
            {"role": "user", "content": '{"query":"六月GMV"}'},
        ],
        "response_format": {"type": "json_object"},
    }
    expected_payload = json.dumps(
        expected_envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert request.prompt_bytes() == expected_payload + bytes(512)


def test_structured_request_repair_preserves_bound_task_without_raw_response() -> None:
    request = StructuredModelRequest.for_output(
        purpose="behavior",
        system_prompt="SYSTEM_SENTINEL Return a decision.",
        user_payload={
            "query": "QUERY_SENTINEL",
            "context": "CONTEXT_SENTINEL",
            "raw_invalid_content": "RAW_RESPONSE_SENTINEL",
        },
        output_type=BehaviorDecision,
        max_output_tokens=128,
    )

    repair = request.for_repair(failure_category=AgentModelErrorCategory.INVALID_STRUCTURE)

    assert request.output_schema_name == "BehaviorDecision"
    assert repair.purpose == "repair"
    assert repair.system_prompt.startswith(request.system_prompt)
    assert repair.output_schema_name == request.output_schema_name
    assert repair.output_schema_summary == request.output_schema_summary
    assert repair.max_output_tokens == request.max_output_tokens
    assert repair.model_dump(mode="json")["user_payload"] == {
        "original_request": {"query": "QUERY_SENTINEL"},
        "failure_category": "invalid_structure",
        "output_schema_name": "BehaviorDecision",
        "re_output_instruction": "Re-output the complete answer as schema-valid JSON only.",
    }
    serialized = repair.model_dump_json()
    assert "SYSTEM_SENTINEL" in serialized
    assert "QUERY_SENTINEL" in serialized
    assert "CONTEXT_SENTINEL" not in serialized
    assert "RAW_RESPONSE_SENTINEL" not in serialized


def test_structured_request_repair_rejects_untrusted_failure_category_safely() -> None:
    sentinel = "https://endpoint.invalid/raw-response/sdk-exception"
    request = StructuredModelRequest.for_output(
        purpose="behavior",
        system_prompt="Return a decision.",
        user_payload={"query": "GMV"},
        output_type=BehaviorDecision,
        max_output_tokens=128,
    )

    with pytest.raises(ValueError) as raised:
        request.for_repair(failure_category=sentinel)

    assert sentinel not in repr(raised.value)


def test_structured_request_rejects_schema_name_summary_mismatch() -> None:
    with pytest.raises(ValidationError):
        StructuredModelRequest(
            purpose="behavior",
            system_prompt="Return a decision.",
            user_payload={"query": "GMV"},
            output_schema_name="BehaviorDecision",
            output_schema_summary={"title": "DifferentOutput", "type": "object"},
            max_output_tokens=128,
        )


@pytest.mark.parametrize("forbidden_key", ["oracle", "expected_rows", "scorer_output"])
def test_structured_request_rejects_evaluation_only_payload_keys(forbidden_key: str) -> None:
    with pytest.raises(ValidationError):
        StructuredModelRequest.for_output(
            purpose="behavior",
            system_prompt="Return a decision.",
            user_payload={forbidden_key: "hidden"},
            output_type=BehaviorDecision,
            max_output_tokens=128,
        )


def test_structured_request_allows_forbidden_words_inside_user_query_text() -> None:
    request = StructuredModelRequest.for_output(
        purpose="behavior",
        system_prompt="Return a decision.",
        user_payload={"query": "Explain expected rows without using an Oracle scorer."},
        output_type=BehaviorDecision,
        max_output_tokens=128,
    )

    assert "Oracle scorer" in cast(str, request.user_payload["query"])


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
            validation_fingerprint="f" * 64,
            valid=True,
            error_code="missing_column",
        )
    with pytest.raises(ValidationError):
        ObservationValidation(
            observation_id="observation-1",
            contract_id="metric_value_contract",
            validation_fingerprint="f" * 64,
            valid=False,
        )


def test_observation_validation_requires_strict_input_fingerprint() -> None:
    with pytest.raises(ValidationError):
        ObservationValidation(  # type: ignore[call-arg]
            observation_id="observation-1",
            contract_id="metric_value_contract",
            valid=True,
        )
    with pytest.raises(ValidationError):
        ObservationValidation(
            observation_id="observation-1",
            contract_id="metric_value_contract",
            validation_fingerprint="not-a-hash",
            valid=True,
        )


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "statement",
        "params",
        "raw_error",
        "result_rows",
        "api_key",
        "access_token",
        "service_url",
        "prompt",
        "sql",
        "oracle",
        "expected_rows",
        "scorer_output",
    ],
)
def test_final_answer_result_summary_rejects_sensitive_aliases(unsafe_key: str) -> None:
    with pytest.raises(ValidationError):
        FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer="done",
            result_summary={"metrics": {unsafe_key: "hidden"}},
        )


@pytest.mark.parametrize("reason", [StopReason.SQL_TIMEOUT, StopReason.TASK_TIMEOUT])
def test_timeout_terminal_statuses_preserve_the_timeout_reason(reason: StopReason) -> None:
    failed = FinalAnswer(
        status=FinalStatus.EXECUTION_FAILED,
        stop_reason=reason,
        answer="timed out",
    )
    partial = FinalAnswer(
        status=FinalStatus.PARTIAL,
        stop_reason=reason,
        answer="partial timeout",
        evidence_ids=("evidence-1",),
    )

    assert failed.stop_reason is reason
    assert partial.stop_reason is reason


def test_task_timeout_is_never_a_budget_exhausted_terminal() -> None:
    with pytest.raises(ValidationError):
        FinalAnswer(
            status=FinalStatus.BUDGET_EXHAUSTED,
            stop_reason=StopReason.TASK_TIMEOUT,
            answer="not a budget outcome",
        )


def _context_observation(*, tool_name: ActionType, ok: bool) -> Observation:
    return Observation(
        observation_id=f"{tool_name.value}-observation",
        tool_name=tool_name,
        purpose="retrieve context",
        ok=ok,
        safe_error=None if ok else "lookup_failed",
    )


def test_context_bundle_requires_canonical_success_order() -> None:
    metric = _context_observation(tool_name=ActionType.METRIC_LOOKUP, ok=True)
    schema = _context_observation(tool_name=ActionType.SCHEMA_LOOKUP, ok=True)

    with pytest.raises(ValidationError):
        ContextBundle(ok=True, metrics=(), tables=(), observations=(schema, metric))


def test_context_bundle_failure_stops_at_first_failed_observation() -> None:
    metric_success = _context_observation(tool_name=ActionType.METRIC_LOOKUP, ok=True)
    metric_failure = _context_observation(tool_name=ActionType.METRIC_LOOKUP, ok=False)
    schema_failure = _context_observation(tool_name=ActionType.SCHEMA_LOOKUP, ok=False)

    with pytest.raises(ValidationError):
        ContextBundle(ok=False, metrics=(), tables=(), observations=(metric_success,))
    with pytest.raises(ValidationError):
        ContextBundle(
            ok=False,
            metrics=(),
            tables=(),
            observations=(metric_failure, schema_failure),
        )


def test_context_bundle_accepts_only_complete_or_first_failure_histories() -> None:
    metric_success = _context_observation(tool_name=ActionType.METRIC_LOOKUP, ok=True)
    metric_failure = _context_observation(tool_name=ActionType.METRIC_LOOKUP, ok=False)
    schema_success = _context_observation(tool_name=ActionType.SCHEMA_LOOKUP, ok=True)
    schema_failure = _context_observation(tool_name=ActionType.SCHEMA_LOOKUP, ok=False)

    assert ContextBundle(
        ok=True,
        metrics=(),
        tables=(),
        observations=(metric_success, schema_success),
    ).ok
    assert not ContextBundle(
        ok=False,
        metrics=(),
        tables=(),
        observations=(metric_failure,),
    ).ok
    assert not ContextBundle(
        ok=False,
        metrics=(),
        tables=(),
        observations=(metric_success, schema_failure),
    ).ok


def test_tool_invocation_requires_all_observation_and_trace_metadata_to_match() -> None:
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
        payload={"rows": ((125,),)},
    )
    matching_trace = ToolCallTrace(
        tool_name=ActionType.EXECUTE_SQL,
        purpose="calculate metric",
        safe_arguments=(("contract_id", "metric_value_contract"),),
        query_id="a" * 64,
        columns=("gmv",),
        row_count=1,
    )

    assert ToolInvocation(observation=observation, trace=matching_trace).observation is observation
    mismatches: tuple[dict[str, object], ...] = (
        {"tool_name": ActionType.PROFILE, "safe_arguments": ()},
        {"purpose": "different purpose"},
        {"query_id": "b" * 64},
        {"columns": ("net_gmv",)},
        {"row_count": 2},
        {"possibly_truncated": True},
        {
            "query_id": None,
            "columns": (),
            "row_count": None,
            "safe_error": "unexpected_error",
        },
    )
    for updates in mismatches:
        mismatched_trace = matching_trace.model_copy(update=updates)
        with pytest.raises(ValidationError, match="metadata must match"):
            ToolInvocation(observation=observation, trace=mismatched_trace)
