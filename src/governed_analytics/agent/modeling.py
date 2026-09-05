"""Budgeted structured-model invocation with one governed repair opportunity."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel

from governed_analytics.agent.contracts import (
    AgentFinishReason,
    AgentModelErrorCategory,
    GovernanceSnapshot,
    ModelCallTrace,
    ModelReservation,
    RepairRecord,
    StopReason,
    StructuredInvocation,
    StructuredModelRequest,
    StructuredModelResult,
)
from governed_analytics.agent.ports import (
    AgentModel,
    AgentModelError,
    BudgetPort,
    Clock,
    StructuredInvocationError,
    TraceRecorder,
)
from governed_analytics.runtime.budgets import BudgetExceeded

STRUCTURE_ERRORS = frozenset(
    {
        AgentModelErrorCategory.INVALID_JSON,
        AgentModelErrorCategory.INVALID_STRUCTURE,
    }
)
_REPAIRABLE_PURPOSES = frozenset({"behavior", "plan", "action", "synthesis"})
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _safe_model(value: object) -> str:
    if (
        isinstance(value, str)
        and _SAFE_IDENTIFIER.fullmatch(value) is not None
        and not value.lower().startswith(("sk-", "pk-", "bearer-"))
    ):
        return value
    return "unknown-model"


def _safe_error_category(error: AgentModelError) -> AgentModelErrorCategory:
    try:
        return AgentModelErrorCategory(error.category)
    except (TypeError, ValueError):
        return AgentModelErrorCategory.PROVIDER_CALL_FAILED


def _safe_nonnegative_integer(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _safe_finish_reason(value: object) -> AgentFinishReason | None:
    return value if isinstance(value, AgentFinishReason) else None


def _stop_reason(category: AgentModelErrorCategory) -> StopReason:
    if category in {
        AgentModelErrorCategory.PROVIDER_CALL_FAILED,
        AgentModelErrorCategory.PROVIDER_HTTP_4XX,
        AgentModelErrorCategory.PROVIDER_RATE_LIMITED,
        AgentModelErrorCategory.PROVIDER_HTTP_5XX,
        AgentModelErrorCategory.PROVIDER_TIMEOUT,
        AgentModelErrorCategory.PROVIDER_CONNECTION_ERROR,
        AgentModelErrorCategory.MISSING_CONTENT,
        AgentModelErrorCategory.INVALID_CONTENT_TYPE,
        AgentModelErrorCategory.MISSING_USAGE,
        AgentModelErrorCategory.INVALID_USAGE,
        AgentModelErrorCategory.MISSING_MODEL,
    }:
        return StopReason.MODEL_UNAVAILABLE
    if (
        category in STRUCTURE_ERRORS
        or category is AgentModelErrorCategory.SCHEMA_IDENTITY_MISMATCH
    ):
        return StopReason.STRUCTURED_OUTPUT_INVALID
    if category is AgentModelErrorCategory.REPAIR_FAILED:
        return StopReason.REPAIR_FAILED
    return StopReason.INTERNAL_ERROR


class StructuredModelInvoker:
    """Own reserve/call/settle/fail/trace and the shared structural repair."""

    def __init__(
        self,
        model: AgentModel,
        budget: BudgetPort,
        trace_recorder: TraceRecorder,
        clock: Clock,
    ) -> None:
        self._model = model
        self._budget = budget
        self._trace_recorder = trace_recorder
        self._clock = clock

    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredInvocation[T]:
        if request.output_schema_name != output_type.__name__:
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.SCHEMA_IDENTITY_MISMATCH,
                traces=(),
                repair_record=None,
                governance=self._budget.snapshot,
                stop_reason=StopReason.STRUCTURED_OUTPUT_INVALID,
            )

        try:
            reservation = self._budget.reserve_model_call(request)
        except BudgetExceeded as error:
            raise self._invocation_error(
                category=AgentModelErrorCategory.BUDGET_EXCEEDED,
                traces=(),
                repair_record=None,
                stop_reason=error.reason,
            ) from None
        except Exception:
            raise self._invocation_error(
                category=AgentModelErrorCategory.BUDGET_RESERVATION_FAILED,
                traces=(),
                repair_record=None,
                stop_reason=StopReason.INTERNAL_ERROR,
            ) from None

        started_at = self._clock.monotonic()
        try:
            result = await self._model.invoke(request, output_type)
        except asyncio.CancelledError:
            self._cancel(reservation, request, started_at=started_at)
            raise
        except AgentModelError as error:
            return await self._handle_first_model_error(
                request=request,
                output_type=output_type,
                reservation=reservation,
                error=error,
            )
        except Exception:
            safe_error = AgentModelError(
                AgentModelErrorCategory.PROVIDER_CALL_FAILED,
                provider_model=_safe_model(self._model.model),
            )
            return await self._handle_first_model_error(
                request=request,
                output_type=output_type,
                reservation=reservation,
                error=safe_error,
            )

        governance, trace = self._settle_or_fail(request, reservation, result)
        self._append(trace)
        if trace.outcome == "failed":
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.ACCOUNTING_CONTRACT_FAILED,
                traces=(trace,),
                repair_record=None,
                governance=governance,
                stop_reason=StopReason.INTERNAL_ERROR,
            )
        return StructuredInvocation(
            result=result,
            traces=(trace,),
            repair_record=None,
            governance=governance,
        )

    async def _handle_first_model_error[T: BaseModel](
        self,
        *,
        request: StructuredModelRequest,
        output_type: type[T],
        reservation: ModelReservation,
        error: AgentModelError,
    ) -> StructuredInvocation[T]:
        category = _safe_error_category(error)
        governance = self._fail_closed(reservation)
        first_trace = self._error_trace(request, reservation, error, outcome="failed")
        self._append(first_trace)
        traces = (first_trace,)

        if not self._allows_repair(request, category, error):
            raise StructuredInvocationError(
                category=category,
                traces=traces,
                repair_record=None,
                governance=governance,
                stop_reason=_stop_reason(category),
            )

        repair_id = uuid4().hex
        try:
            governance = self._budget.consume_repair()
        except BudgetExceeded as blocked:
            record = self._repair_record(
                repair_id, request, category, outcome="blocked"
            )
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.REPAIR_BLOCKED,
                traces=traces,
                repair_record=record,
                governance=self._budget.snapshot,
                stop_reason=blocked.reason,
            ) from None
        except Exception:
            record = self._repair_record(
                repair_id, request, category, outcome="blocked"
            )
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.REPAIR_BLOCKED,
                traces=traces,
                repair_record=record,
                governance=self._budget.snapshot,
                stop_reason=StopReason.INTERNAL_ERROR,
            ) from None

        repair_request = request.for_repair(failure_category=category)
        try:
            repair_reservation = self._budget.reserve_model_call(repair_request)
        except BudgetExceeded as blocked:
            record = self._repair_record(
                repair_id, request, category, outcome="blocked"
            )
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.REPAIR_BLOCKED,
                traces=traces,
                repair_record=record,
                governance=self._budget.snapshot,
                stop_reason=blocked.reason,
            ) from None
        except Exception:
            record = self._repair_record(
                repair_id, request, category, outcome="blocked"
            )
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.REPAIR_BLOCKED,
                traces=traces,
                repair_record=record,
                governance=governance,
                stop_reason=StopReason.INTERNAL_ERROR,
            ) from None

        started_at = self._clock.monotonic()
        try:
            repaired_result = await self._model.invoke(repair_request, output_type)
        except asyncio.CancelledError:
            self._cancel(repair_reservation, repair_request, started_at=started_at)
            raise
        except AgentModelError as repair_error:
            repair_category = _safe_error_category(repair_error)
            governance = self._fail_closed(repair_reservation)
            repair_trace = self._error_trace(
                repair_request,
                repair_reservation,
                repair_error,
                outcome="failed",
            )
            self._append(repair_trace)
            record = self._repair_record(
                repair_id, request, category, outcome="failed"
            )
            raise StructuredInvocationError(
                category=repair_category,
                traces=(first_trace, repair_trace),
                repair_record=record,
                governance=governance,
                stop_reason=StopReason.REPAIR_FAILED,
            ) from None
        except Exception:
            governance = self._fail_closed(repair_reservation)
            safe_error = AgentModelError(
                AgentModelErrorCategory.PROVIDER_CALL_FAILED,
                provider_model=_safe_model(self._model.model),
            )
            repair_trace = self._error_trace(
                repair_request,
                repair_reservation,
                safe_error,
                outcome="failed",
            )
            self._append(repair_trace)
            record = self._repair_record(
                repair_id, request, category, outcome="failed"
            )
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.PROVIDER_CALL_FAILED,
                traces=(first_trace, repair_trace),
                repair_record=record,
                governance=governance,
                stop_reason=StopReason.REPAIR_FAILED,
            ) from None

        governance, repair_trace = self._settle_or_fail(
            repair_request, repair_reservation, repaired_result
        )
        self._append(repair_trace)
        if repair_trace.outcome == "failed":
            record = self._repair_record(
                repair_id, request, category, outcome="failed"
            )
            raise StructuredInvocationError(
                category=AgentModelErrorCategory.ACCOUNTING_CONTRACT_FAILED,
                traces=(first_trace, repair_trace),
                repair_record=record,
                governance=governance,
                stop_reason=StopReason.REPAIR_FAILED,
            )
        record = self._repair_record(repair_id, request, category, outcome="success")
        return StructuredInvocation(
            result=repaired_result,
            traces=(first_trace, repair_trace),
            repair_record=record,
            governance=governance,
        )

    def _settle_or_fail[T: BaseModel](
        self,
        request: StructuredModelRequest,
        reservation: ModelReservation,
        result: StructuredModelResult[T],
    ) -> tuple[GovernanceSnapshot, ModelCallTrace]:
        before = self._budget.snapshot
        try:
            governance = self._budget.settle_model_call(
                reservation,
                result.usage,
                result.provider_model,
            )
        except Exception:
            governance = self._fail_closed(reservation)
            return governance, ModelCallTrace(
                purpose=request.purpose,
                provider_model=_safe_model(result.provider_model),
                outcome="failed",
                safe_error=AgentModelErrorCategory.ACCOUNTING_CONTRACT_FAILED.value,
                latency_ms=result.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                finish_reason=result.finish_reason,
                output_truncated=result.output_truncated,
                estimated_cost_cny=reservation.reserved_cost_cny,
            )
        return governance, ModelCallTrace(
            purpose=request.purpose,
            provider_model=_safe_model(result.provider_model),
            outcome="completed",
            safe_error=None,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            finish_reason=result.finish_reason,
            output_truncated=result.output_truncated,
            estimated_cost_cny=max(
                Decimal("0"),
                governance.committed_cost_cny - before.committed_cost_cny,
            ),
        )

    def _cancel(
        self,
        reservation: ModelReservation,
        request: StructuredModelRequest,
        *,
        started_at: float,
    ) -> None:
        self._fail_closed(reservation)
        trace = ModelCallTrace(
            purpose=request.purpose,
            provider_model=_safe_model(self._model.model),
            outcome="cancelled",
            safe_error=AgentModelErrorCategory.CANCELLED.value,
            latency_ms=max(0, round((self._clock.monotonic() - started_at) * 1000)),
            input_tokens=0,
            output_tokens=0,
            finish_reason=None,
            output_truncated=False,
            estimated_cost_cny=reservation.reserved_cost_cny,
        )
        self._append(trace)

    def _error_trace(
        self,
        request: StructuredModelRequest,
        reservation: ModelReservation,
        error: AgentModelError,
        *,
        outcome: str,
    ) -> ModelCallTrace:
        category = _safe_error_category(error)
        finish_reason = _safe_finish_reason(error.finish_reason)
        return ModelCallTrace(
            purpose=request.purpose,
            provider_model=_safe_model(error.provider_model or self._model.model),
            outcome=outcome,  # type: ignore[arg-type]
            safe_error=category.value,
            latency_ms=_safe_nonnegative_integer(error.latency_ms),
            input_tokens=_safe_nonnegative_integer(error.input_tokens),
            output_tokens=_safe_nonnegative_integer(error.output_tokens),
            finish_reason=finish_reason,
            output_truncated=finish_reason is AgentFinishReason.LENGTH,
            estimated_cost_cny=reservation.reserved_cost_cny,
        )

    def _fail_closed(self, reservation: ModelReservation) -> GovernanceSnapshot:
        try:
            return self._budget.fail_model_call(reservation)
        except Exception:
            return self._budget.snapshot

    def _append(self, trace: ModelCallTrace) -> None:
        self._trace_recorder.append_model((trace,))

    @staticmethod
    def _allows_repair(
        request: StructuredModelRequest,
        category: AgentModelErrorCategory,
        error: AgentModelError,
    ) -> bool:
        finish_reason = _safe_finish_reason(error.finish_reason)
        return (
            request.purpose in _REPAIRABLE_PURPOSES
            and category in STRUCTURE_ERRORS
            and finish_reason
            not in {AgentFinishReason.LENGTH, AgentFinishReason.CONTENT_FILTER}
        )

    @staticmethod
    def _repair_record(
        repair_id: str,
        request: StructuredModelRequest,
        error_code: AgentModelErrorCategory,
        *,
        outcome: str,
    ) -> RepairRecord:
        return RepairRecord(
            repair_id=repair_id,
            kind="structured_output",
            target=request.output_schema_name,
            error_code=error_code.value,
            outcome=outcome,  # type: ignore[arg-type]
        )

    def _invocation_error(
        self,
        *,
        category: AgentModelErrorCategory,
        traces: tuple[ModelCallTrace, ...],
        repair_record: RepairRecord | None,
        stop_reason: StopReason,
    ) -> StructuredInvocationError:
        return StructuredInvocationError(
            category=category,
            traces=traces,
            repair_record=repair_record,
            governance=self._budget.snapshot,
            stop_reason=stop_reason,
        )


__all__ = ["STRUCTURE_ERRORS", "StructuredModelInvoker"]
