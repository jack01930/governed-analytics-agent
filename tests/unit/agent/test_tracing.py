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
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    ModelCallTrace,
    NodeTrace,
    Observation,
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
