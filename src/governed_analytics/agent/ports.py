"""Dependency ports injected into the agent graph outside mutable State."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    AgentFinishReason,
    GovernanceSnapshot,
    JsonValue,
    ModelCallTrace,
    ModelReservation,
    ModelUsage,
    NodeTrace,
    RepairRecord,
    SafeTrace,
    StopReason,
    StructuredInvocation,
    StructuredModelRequest,
    StructuredModelResult,
    ToolCallTrace,
    ToolInvocation,
)


class AgentModelError(ValueError):
    """Stable Agent-model failure carrying only safe call-accounting metadata."""

    def __init__(
        self,
        category: str,
        *,
        provider_model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        finish_reason: AgentFinishReason | None = None,
        output_truncated: bool = False,
    ) -> None:
        if not category.strip():
            raise ValueError("category must not be blank")
        self.category = category
        self.provider_model = provider_model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.finish_reason = finish_reason
        self.output_truncated = output_truncated
        super().__init__(category)


class AgentModel(Protocol):
    @property
    def model(self) -> str:
        raise NotImplementedError

    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredModelResult[T]:
        raise NotImplementedError


class BudgetPort(Protocol):
    @property
    def snapshot(self) -> GovernanceSnapshot:
        raise NotImplementedError

    def reserve_model_call(self, request: StructuredModelRequest) -> ModelReservation:
        raise NotImplementedError

    def settle_model_call(
        self,
        reservation: ModelReservation,
        usage: ModelUsage,
        provider_model: str,
    ) -> GovernanceSnapshot:
        raise NotImplementedError

    def fail_model_call(self, reservation: ModelReservation) -> GovernanceSnapshot:
        raise NotImplementedError

    def consume_tool(self, action_type: ActionType) -> GovernanceSnapshot:
        raise NotImplementedError

    def ensure_action_loop_available(self) -> None:
        raise NotImplementedError

    def consume_action_loop(self) -> GovernanceSnapshot:
        raise NotImplementedError

    def consume_repair(self) -> GovernanceSnapshot:
        raise NotImplementedError

    def ensure_time_remaining(self) -> None:
        raise NotImplementedError


class EventSink(Protocol):
    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        raise NotImplementedError


class Clock(Protocol):
    def now(self) -> datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError


class TraceRecorder(Protocol):
    def append_node(self, trace: NodeTrace) -> None:
        raise NotImplementedError

    def append_model(self, traces: tuple[ModelCallTrace, ...]) -> None:
        raise NotImplementedError

    def append_tool(self, trace: ToolCallTrace) -> None:
        raise NotImplementedError

    def snapshot(self) -> SafeTrace:
        raise NotImplementedError


class ModelInvoker(Protocol):
    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredInvocation[T]:
        raise NotImplementedError


class AgentTools(Protocol):
    async def lookup_metrics(self, *, node: Literal["retrieve_context"]) -> ToolInvocation:
        raise NotImplementedError

    async def lookup_schema(self, *, node: Literal["retrieve_context"]) -> ToolInvocation:
        raise NotImplementedError

    async def invoke(self, action: AgentAction, *, node: str) -> ToolInvocation:
        raise NotImplementedError


class StructuredInvocationError(Exception):
    """Safe model-invocation failure with only governed diagnostic fields."""

    category: str
    traces: tuple[ModelCallTrace, ...]
    repair_record: RepairRecord | None
    governance: GovernanceSnapshot
    stop_reason: StopReason

    def __init__(
        self,
        *,
        category: str,
        traces: tuple[ModelCallTrace, ...],
        repair_record: RepairRecord | None,
        governance: GovernanceSnapshot,
        stop_reason: StopReason,
    ) -> None:
        if not category.strip():
            raise ValueError("category must not be blank")
        self.category = category
        self.traces = traces
        self.repair_record = repair_record
        self.governance = governance
        self.stop_reason = stop_reason
        super().__init__(category)


@dataclass(frozen=True, kw_only=True)
class AgentContext:
    model_invoker: ModelInvoker
    tools: AgentTools
    budget: BudgetPort
    events: EventSink
    trace_recorder: TraceRecorder
    clock: Clock


__all__ = [
    "AgentContext",
    "AgentModel",
    "AgentModelError",
    "AgentTools",
    "BudgetPort",
    "Clock",
    "EventSink",
    "ModelInvoker",
    "StructuredInvocationError",
    "TraceRecorder",
]
