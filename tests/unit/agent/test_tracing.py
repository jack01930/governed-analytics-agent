from decimal import Decimal

import pytest
from pydantic import ValidationError

from governed_analytics.agent.contracts import (
    ActionType,
    AgentFinishReason,
    AgentRunResult,
    BehaviorAction,
    BehaviorDecision,
    BehaviorReasonCode,
    EvidenceItem,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    ModelCallTrace,
    NodeTrace,
    Observation,
    ObservationValidation,
    SafeTrace,
    StopReason,
    ToolCallTrace,
)
from governed_analytics.agent.tracing import InMemoryTraceRecorder


def test_trace_recorder_is_append_only_and_returns_frozen_snapshot() -> None:
    recorder = InMemoryTraceRecorder()
    node = NodeTrace(node="route_action", duration_ms=2, outcome="completed")
    model = ModelCallTrace(
        purpose="action",
        provider_model="fixture-model",
        outcome="completed",
        latency_ms=3,
        input_tokens=10,
        output_tokens=5,
        finish_reason=AgentFinishReason.STOP,
        output_truncated=False,
        estimated_cost_cny=Decimal("0.001"),
    )
    tool = ToolCallTrace(
        tool_name=ActionType.EXECUTE_SQL,
        purpose="calculate metric",
        safe_arguments=(("contract_id", "metric_value_contract"),),
        query_id="a" * 64,
        columns=("gmv",),
        row_count=1,
    )

    recorder.append_node(node)
    recorder.append_model((model,))
    recorder.append_tool(tool)

    snapshot = recorder.snapshot()
    assert snapshot == SafeTrace(nodes=(node,), model_calls=(model,), tool_calls=(tool,))
    assert recorder.snapshot() is not snapshot


def test_agent_run_result_keeps_internal_observations_but_safe_trace_has_no_payload() -> None:
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
        payload={"rows": [[125]]},  # type: ignore[dict-item]
    )
    result = AgentRunResult(
        run_id="run-1",
        behavior=BehaviorDecision(
            action=BehaviorAction.EXECUTE,
            reason_code=BehaviorReasonCode.READY,
            user_message="ready",
        ),
        answer_contract=None,
        observations=(observation,),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=observation,
        repair_history=(),
        governance=GovernanceSnapshot(),
        final_answer=FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer="GMV 为 125。",
        ),
        safe_trace=SafeTrace(
            tool_calls=(
                ToolCallTrace(
                    tool_name=ActionType.EXECUTE_SQL,
                    purpose="calculate metric",
                    safe_arguments=(),
                    query_id="a" * 64,
                    columns=("gmv",),
                    row_count=1,
                ),
            )
        ),
    )

    assert result.observations[0].payload == {"rows": ((125,),)}
    trace_dump = result.safe_trace.model_dump(mode="json")
    assert "payload" not in str(trace_dump)
    assert "rows" not in str(trace_dump)


def test_safe_trace_rejects_payload_or_rows_in_safe_arguments() -> None:
    with pytest.raises(ValidationError):
        ToolCallTrace(
            tool_name=ActionType.EXECUTE_SQL,
            purpose="calculate metric",
            safe_arguments=(("payload", {"rows": ((125,),)}),),
        )


@pytest.mark.parametrize(
    "unsafe_key",
    ["statement", "params", "raw_error", "result_rows", "api_key"],
)
def test_tool_trace_rejects_unknown_or_aliased_safe_arguments(unsafe_key: str) -> None:
    with pytest.raises(ValidationError):
        ToolCallTrace(
            tool_name=ActionType.EXECUTE_SQL,
            purpose="calculate metric",
            safe_arguments=((unsafe_key, "hidden"),),
        )


def test_profile_trace_allowlist_rejects_unsafe_nested_shapes() -> None:
    with pytest.raises(ValidationError):
        ToolCallTrace(
            tool_name=ActionType.PROFILE,
            purpose="profile context",
            safe_arguments=(("filter_columns", ({"api_key": "hidden"},)),),
        )
    with pytest.raises(ValidationError):
        ToolCallTrace(
            tool_name=ActionType.PROFILE,
            purpose="profile context",
            safe_arguments=(("limit", True),),
        )
    with pytest.raises(ValidationError):
        ToolCallTrace(
            tool_name=ActionType.PROFILE,
            purpose="profile context",
            safe_arguments=(("operation", {"statement": "select 1"}),),
        )


def _verified_evidence(*, query_id: str = "a" * 64, verified: bool = True) -> EvidenceItem:
    return EvidenceItem(
        evidence_id="evidence-1",
        observation_id="observation-1",
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        query_id=query_id,
        claim_key="gmv",
        stance="supports",
        numeric_value=Decimal("125"),
        unit="CNY",
        verified=verified,
    )


def _run_result(
    *,
    observation: Observation,
    validation: ObservationValidation,
    evidence: EvidenceItem,
    final_evidence_ids: tuple[str, ...] = ("evidence-1",),
) -> AgentRunResult:
    return AgentRunResult(
        run_id="run-1",
        behavior=None,
        answer_contract=None,
        observations=(observation,),
        observation_validations=(validation,),
        evidence=(evidence,),
        evidence_gaps=(),
        first_candidate=observation,
        repair_history=(),
        governance=GovernanceSnapshot(),
        final_answer=FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer="GMV is 125.",
            evidence_ids=final_evidence_ids,
        ),
        safe_trace=SafeTrace(),
    )


def test_agent_run_result_rejects_validation_for_non_execute_or_wrong_contract() -> None:
    profile = Observation(
        observation_id="observation-1",
        tool_name=ActionType.PROFILE,
        purpose="profile context",
        ok=True,
        query_id="a" * 64,
        columns=("gmv",),
        row_count=1,
        payload={"rows": ((125,),)},
    )
    validation = ObservationValidation(
        observation_id="observation-1",
        contract_id="metric_value_contract",
        validation_fingerprint="f" * 64,
        valid=True,
    )

    with pytest.raises(ValidationError):
        _run_result(observation=profile, validation=validation, evidence=_verified_evidence())

    execute = Observation(
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
    wrong_contract = validation.model_copy(update={"contract_id": "other_contract"})
    with pytest.raises(ValidationError):
        _run_result(
            observation=execute,
            validation=wrong_contract,
            evidence=_verified_evidence(),
        )


def test_agent_run_result_rejects_verified_evidence_without_matching_valid_validation() -> None:
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
    invalid_validation = ObservationValidation(
        observation_id="observation-1",
        contract_id="metric_value_contract",
        validation_fingerprint="f" * 64,
        valid=False,
        error_code="wrong_shape",
    )

    valid_validation = invalid_validation.model_copy(update={"valid": True, "error_code": None})
    assert (
        _run_result(
            observation=observation,
            validation=valid_validation,
            evidence=_verified_evidence(),
        )
        .evidence[0]
        .verified
    )

    with pytest.raises(ValidationError):
        _run_result(
            observation=observation,
            validation=invalid_validation,
            evidence=_verified_evidence(),
        )

    mismatched_evidence = (
        _verified_evidence(query_id="b" * 64),
        _verified_evidence().model_copy(update={"contract_id": "other_contract"}),
        _verified_evidence().model_copy(update={"hypothesis_id": "other_hypothesis"}),
        _verified_evidence().model_copy(update={"observation_id": "other_observation"}),
    )
    for evidence in mismatched_evidence:
        with pytest.raises(ValidationError):
            _run_result(
                observation=observation,
                validation=valid_validation,
                evidence=evidence,
            )


def test_agent_run_result_final_answer_only_references_verified_evidence() -> None:
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
    validation = ObservationValidation(
        observation_id="observation-1",
        contract_id="metric_value_contract",
        validation_fingerprint="f" * 64,
        valid=True,
    )

    with pytest.raises(ValidationError):
        _run_result(
            observation=observation,
            validation=validation,
            evidence=_verified_evidence(verified=False),
        )
    with pytest.raises(ValidationError):
        _run_result(
            observation=observation,
            validation=validation,
            evidence=_verified_evidence(),
            final_evidence_ids=("unknown-evidence",),
        )
