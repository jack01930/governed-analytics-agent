from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from governed_analytics.agent.contracts import (
    AgentFinishReason,
    AgentModelErrorCategory,
    BehaviorAction,
    BehaviorDecision,
    BehaviorReasonCode,
    ModelUsage,
    StopReason,
    StructuredModelRequest,
    StructuredModelResult,
)
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.ports import AgentModelError, StructuredInvocationError
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.pricing import ModelPricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 9, 4, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0


def _decision() -> BehaviorDecision:
    return BehaviorDecision(
        action=BehaviorAction.EXECUTE,
        reason_code=BehaviorReasonCode.READY,
        missing_fields=(),
        user_message="开始分析。",
    )


def _result() -> StructuredModelResult[BehaviorDecision]:
    return StructuredModelResult(
        output=_decision(),
        provider_model="fixture-agent",
        usage=ModelUsage(input_tokens=0, output_tokens=0),
        latency_ms=2,
        finish_reason=AgentFinishReason.STOP,
        output_truncated=False,
    )


def _result_with_model(provider_model: str) -> StructuredModelResult[BehaviorDecision]:
    return _result().model_copy(update={"provider_model": provider_model})


class _Model:
    model = "fixture-agent"

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[StructuredModelRequest] = []

    async def invoke[T](self, request: StructuredModelRequest, output_type: type[T]) -> object:
        del output_type
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _CancelledModel:
    model = "fixture-agent"

    async def invoke[T](self, request: StructuredModelRequest, output_type: type[T]) -> object:
        del request, output_type
        raise asyncio.CancelledError


def _request(
    *, purpose: str = "behavior", schema_name: str = "BehaviorDecision"
) -> StructuredModelRequest:
    return StructuredModelRequest(
        purpose=purpose,  # type: ignore[arg-type]
        system_prompt="contract",
        user_payload={"query": "六月GMV"},
        output_schema_name=schema_name,
        output_schema_summary={"title": schema_name, "type": "object"},
        max_output_tokens=300,
    )


def _budget(*, max_repairs: int = 1) -> BudgetLedger:
    return BudgetLedger(
        limits=BudgetLimits(
            max_action_loops=4,
            max_llm_calls=8,
            max_tool_calls=12,
            max_execute_calls=5,
            max_profile_calls=2,
            max_repairs=max_repairs,
            timeout_seconds=60,
            soft_cost_cny=Decimal("1"),
            hard_cost_cny=Decimal("2"),
        ),
        pricing=ModelPricing.model_validate(
            {
                "provider": "fixture",
                "region": "local",
                "requested_model": "fixture-agent",
                "resolved_model": "fixture-agent",
                "effective_date": date(2026, 9, 1),
                "currency": "CNY",
                "unit_tokens": 1000,
                "input_token_upper_bound": 10000,
                "input_price": "0",
                "output_price": "0",
                "pricing_basis": "test",
                "source": "https://example.test/pricing",
            }
        ),
        monotonic=lambda: 0.0,
    )


def _invoker(
    model: object,
    budget: BudgetLedger,
    recorder: InMemoryTraceRecorder,
) -> StructuredModelInvoker:
    return StructuredModelInvoker(model, budget, recorder, _Clock())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_structural_error_repairs_once_and_returns_all_metadata() -> None:
    model = _Model([AgentModelError("invalid_structure"), _result()])
    budget = _budget()
    recorder = InMemoryTraceRecorder()

    invocation = await _invoker(model, budget, recorder).invoke(_request(), BehaviorDecision)

    assert [request.purpose for request in model.calls] == ["behavior", "repair"]
    assert invocation.result.output.action == "execute"
    assert invocation.repair_record is not None
    assert invocation.repair_record.outcome == "success"
    assert invocation.governance.repair_count == 1
    assert invocation.governance.structured_output_repair_count == 1
    assert len(invocation.traces) == 2
    assert recorder.snapshot().model_calls == invocation.traces


@pytest.mark.asyncio
async def test_repair_call_receives_only_fixed_safe_payload() -> None:
    model = _Model([AgentModelError("invalid_structure"), _result()])
    request = StructuredModelRequest(
        purpose="behavior",
        system_prompt="SYSTEM_SENTINEL",
        user_payload={
            "query": "QUERY_SENTINEL",
            "context": "CONTEXT_SENTINEL",
            "raw_invalid_content": "RAW_RESPONSE_SENTINEL",
        },
        output_schema_name="BehaviorDecision",
        output_schema_summary={"title": "BehaviorDecision", "type": "object"},
        max_output_tokens=300,
    )

    await _invoker(model, _budget(), InMemoryTraceRecorder()).invoke(
        request, BehaviorDecision
    )

    repair = model.calls[1]
    assert repair.purpose == "repair"
    assert repair.system_prompt == (
        "Return exactly one JSON object that conforms to the bound output schema."
    )
    assert dict(repair.user_payload) == {
        "failure_category": "invalid_structure",
        "output_schema_name": "BehaviorDecision",
        "re_output_instruction": "Re-output the complete answer as schema-valid JSON only.",
    }
    serialized = repair.model_dump_json()
    assert "SYSTEM_SENTINEL" not in serialized
    assert "QUERY_SENTINEL" not in serialized
    assert "CONTEXT_SENTINEL" not in serialized
    assert "RAW_RESPONSE_SENTINEL" not in serialized


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider_model": "https://endpoint.invalid/sk-secret"},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"latency_ms": -1},
    ],
)
def test_agent_model_error_rejects_unsafe_metadata_without_echo(
    kwargs: dict[str, object],
) -> None:
    sentinel = next((str(value) for value in kwargs.values() if isinstance(value, str)), "")

    with pytest.raises(ValueError) as raised:
        AgentModelError(AgentModelErrorCategory.PROVIDER_CALL_FAILED, **kwargs)  # type: ignore[arg-type]

    assert not sentinel or sentinel not in repr(raised.value)


def test_agent_model_error_rejects_untrusted_category_without_echo() -> None:
    sentinel = "https://endpoint.invalid/raw-response/sdk-exception"

    with pytest.raises(ValueError) as raised:
        AgentModelError(sentinel)

    assert sentinel not in repr(raised.value)


@pytest.mark.asyncio
async def test_invoker_normalizes_mutated_untrusted_error_before_safe_outputs() -> None:
    category_sentinel = "https://endpoint.invalid/raw-response/sdk-exception"
    model_sentinel = "sk-secret-provider/raw-response"
    error = AgentModelError(AgentModelErrorCategory.INVALID_STRUCTURE)
    error.category = category_sentinel  # type: ignore[assignment]
    error.provider_model = model_sentinel
    model = _Model([error])

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, _budget(), InMemoryTraceRecorder()).invoke(
            _request(), BehaviorDecision
        )

    safe_serialized = repr(
        (
            repr(raised.value),
            raised.value.category,
            raised.value.repair_record,
            tuple(trace.model_dump_json() for trace in raised.value.traces),
        )
    )
    assert raised.value.category == AgentModelErrorCategory.PROVIDER_CALL_FAILED
    assert raised.value.repair_record is None
    assert category_sentinel not in safe_serialized
    assert model_sentinel not in safe_serialized


@pytest.mark.asyncio
async def test_failed_repair_never_attempts_a_third_call() -> None:
    model = _Model(
        [
            AgentModelError("invalid_json"),
            AgentModelError("invalid_structure"),
            _result(),
        ]
    )
    recorder = InMemoryTraceRecorder()

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, _budget(), recorder).invoke(_request(), BehaviorDecision)

    assert len(model.calls) == 2
    assert raised.value.repair_record is not None
    assert raised.value.repair_record.outcome == "failed"
    assert len(raised.value.traces) == 2


@pytest.mark.asyncio
async def test_shared_repair_is_not_consumed_twice_across_invocations() -> None:
    model = _Model(
        [
            AgentModelError("invalid_structure"),
            _result(),
            AgentModelError("invalid_structure"),
            _result(),
        ]
    )
    invoker = _invoker(model, _budget(), InMemoryTraceRecorder())

    first = await invoker.invoke(_request(), BehaviorDecision)
    with pytest.raises(StructuredInvocationError) as second:
        await invoker.invoke(_request(), BehaviorDecision)

    assert first.repair_record is not None
    assert first.repair_record.outcome == "success"
    assert second.value.repair_record is not None
    assert second.value.repair_record.outcome == "blocked"
    assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_request_already_for_repair_never_nests_repair() -> None:
    model = _Model([AgentModelError("invalid_json"), _result()])

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, _budget(), InMemoryTraceRecorder()).invoke(
            _request(purpose="repair"), BehaviorDecision
        )

    assert len(model.calls) == 1
    assert raised.value.repair_record is None
    assert raised.value.governance.repair_count == 0


@pytest.mark.asyncio
async def test_blocked_repair_has_one_trace_and_no_second_model_call() -> None:
    model = _Model([AgentModelError("invalid_structure"), _result()])

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, _budget(max_repairs=0), InMemoryTraceRecorder()).invoke(
            _request(), BehaviorDecision
        )

    assert len(model.calls) == 1
    assert raised.value.repair_record is not None
    assert raised.value.repair_record.outcome == "blocked"
    assert len(raised.value.traces) == 1


@pytest.mark.asyncio
async def test_schema_mismatch_performs_neither_reservation_nor_provider_call() -> None:
    model = _Model([_result()])
    budget = _budget()

    with pytest.raises(StructuredInvocationError, match=r"^schema_identity_mismatch$") as raised:
        await _invoker(model, budget, InMemoryTraceRecorder()).invoke(
            _request(schema_name="OtherDecision"), BehaviorDecision
        )

    assert model.calls == []
    assert budget.snapshot.llm_calls == 0
    assert raised.value.traces == ()


@pytest.mark.asyncio
async def test_non_structural_error_is_not_repaired() -> None:
    error = AgentModelError(
        "provider_call_failed",
        provider_model="fixture-agent",
        latency_ms=3,
        finish_reason=AgentFinishReason.CONTENT_FILTER,
    )
    model = _Model([error, _result()])

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, _budget(), InMemoryTraceRecorder()).invoke(
            _request(), BehaviorDecision
        )

    assert len(model.calls) == 1
    assert raised.value.stop_reason is StopReason.MODEL_UNAVAILABLE
    assert raised.value.repair_record is None


@pytest.mark.asyncio
async def test_length_truncation_is_never_structurally_repaired() -> None:
    error = AgentModelError(
        "invalid_json",
        provider_model="fixture-agent",
        finish_reason=AgentFinishReason.LENGTH,
        output_truncated=True,
    )
    model = _Model([error, _result()])

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, _budget(), InMemoryTraceRecorder()).invoke(
            _request(), BehaviorDecision
        )

    assert len(model.calls) == 1
    assert raised.value.traces[0].finish_reason is AgentFinishReason.LENGTH
    assert raised.value.repair_record is None


@pytest.mark.asyncio
async def test_initial_settle_identity_failure_fails_closed_without_repair() -> None:
    budget = _budget()
    model = _Model([_result_with_model("different-model")])

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, budget, InMemoryTraceRecorder()).invoke(
            _request(), BehaviorDecision
        )

    assert raised.value.category == "accounting_contract_failed"
    assert raised.value.repair_record is None
    assert raised.value.traces[0].outcome == "failed"
    assert budget.snapshot.reserved_cost_cny == Decimal("0")


@pytest.mark.asyncio
async def test_repair_settle_identity_failure_is_a_failed_repair() -> None:
    budget = _budget()
    model = _Model(
        [AgentModelError("invalid_structure"), _result_with_model("different-model")]
    )

    with pytest.raises(StructuredInvocationError) as raised:
        await _invoker(model, budget, InMemoryTraceRecorder()).invoke(
            _request(), BehaviorDecision
        )

    assert raised.value.repair_record is not None
    assert raised.value.repair_record.outcome == "failed"
    assert len(raised.value.traces) == 2
    assert raised.value.traces[-1].safe_error == "accounting_contract_failed"
    assert budget.snapshot.reserved_cost_cny == Decimal("0")


@pytest.mark.asyncio
async def test_cancellation_during_repair_preserves_first_and_cancelled_traces() -> None:
    model = _Model([AgentModelError("invalid_structure"), asyncio.CancelledError()])
    recorder = InMemoryTraceRecorder()

    with pytest.raises(asyncio.CancelledError):
        await _invoker(model, _budget(), recorder).invoke(_request(), BehaviorDecision)

    assert [trace.outcome for trace in recorder.snapshot().model_calls] == [
        "failed",
        "cancelled",
    ]
    assert recorder.snapshot().model_calls[-1].purpose == "repair"


@pytest.mark.asyncio
async def test_cancellation_fails_closed_records_safe_trace_and_reraises() -> None:
    budget = _budget()
    recorder = InMemoryTraceRecorder()

    with pytest.raises(asyncio.CancelledError):
        await _invoker(_CancelledModel(), budget, recorder).invoke(_request(), BehaviorDecision)

    assert budget.snapshot.reserved_cost_cny == Decimal("0")
    assert len(recorder.snapshot().model_calls) == 1
    trace = recorder.snapshot().model_calls[0]
    assert trace.outcome == "cancelled"
    assert trace.safe_error == "cancelled"
