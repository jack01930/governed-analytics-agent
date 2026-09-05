"""Fail-closed runtime budget reservation and accounting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic as system_monotonic
from uuid import uuid4

from governed_analytics.agent.contracts import (
    ActionType,
    GovernanceSnapshot,
    ModelReservation,
    ModelUsage,
    StopReason,
    StructuredModelRequest,
)
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.pricing import (
    ModelPricing,
    estimate_cost_cny,
    provider_model_has_pricing,
)


@dataclass(frozen=True)
class BudgetLimits:
    max_action_loops: int
    max_llm_calls: int
    max_tool_calls: int
    max_execute_calls: int
    max_profile_calls: int
    max_repairs: int
    timeout_seconds: int
    soft_cost_cny: Decimal
    hard_cost_cny: Decimal

    @classmethod
    def from_settings(cls, settings: AgentRuntimeSettings) -> BudgetLimits:
        return cls(
            max_action_loops=settings.max_action_loops,
            max_llm_calls=settings.max_llm_calls,
            max_tool_calls=settings.max_tool_calls,
            max_execute_calls=settings.max_execute_calls,
            max_profile_calls=settings.max_profile_calls,
            max_repairs=settings.max_repairs,
            timeout_seconds=settings.timeout_seconds,
            soft_cost_cny=settings.soft_cost_cny,
            hard_cost_cny=settings.hard_cost_cny,
        )


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: StopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class BudgetLedger:
    """Own reservations and atomically expose immutable governance snapshots."""

    def __init__(
        self,
        *,
        limits: BudgetLimits,
        pricing: ModelPricing,
        monotonic: Callable[[], float] = system_monotonic,
    ) -> None:
        self._limits = limits
        self._pricing = pricing
        self._monotonic = monotonic
        self._reservations: dict[str, ModelReservation] = {}
        self._snapshot = GovernanceSnapshot(
            deadline_monotonic=monotonic() + limits.timeout_seconds
        )

    @property
    def snapshot(self) -> GovernanceSnapshot:
        return self._snapshot

    def reserve_model_call(self, request: StructuredModelRequest) -> ModelReservation:
        self.ensure_time_remaining()
        if self._snapshot.llm_calls >= self._limits.max_llm_calls:
            raise BudgetExceeded(StopReason.LLM_CALL_LIMIT)
        input_upper = len(request.prompt_bytes())
        projected = estimate_cost_cny(input_upper, request.max_output_tokens, self._pricing)
        if (
            self._snapshot.committed_cost_cny
            + self._snapshot.reserved_cost_cny
            + projected
            > self._limits.hard_cost_cny
        ):
            raise BudgetExceeded(StopReason.COST_HARD_CAP)
        reservation = ModelReservation(
            reservation_id=uuid4().hex,
            input_token_upper_bound=input_upper,
            output_token_upper_bound=request.max_output_tokens,
            reserved_cost_cny=projected,
        )
        self._reservations[reservation.reservation_id] = reservation
        self._snapshot = self._snapshot.model_copy(
            update={
                "llm_calls": self._snapshot.llm_calls + 1,
                "reserved_cost_cny": self._snapshot.reserved_cost_cny + projected,
            }
        )
        return reservation

    def settle_model_call(
        self,
        reservation: ModelReservation,
        usage: ModelUsage,
        provider_model: str,
    ) -> GovernanceSnapshot:
        active = self._require_active(reservation)
        if not provider_model_has_pricing(provider_model, self._pricing):
            raise RuntimeError("provider model does not match pricing contract")
        try:
            actual_cost = estimate_cost_cny(
                usage.input_tokens,
                usage.output_tokens,
                self._pricing,
            )
        except ValueError:
            raise RuntimeError("usage exceeds reserved cost") from None
        if actual_cost > active.reserved_cost_cny:
            raise RuntimeError("usage exceeds reserved cost")
        del self._reservations[active.reservation_id]
        committed = self._snapshot.committed_cost_cny + actual_cost
        self._snapshot = self._snapshot.model_copy(
            update={
                "input_tokens": self._snapshot.input_tokens + usage.input_tokens,
                "output_tokens": self._snapshot.output_tokens + usage.output_tokens,
                "committed_cost_cny": committed,
                "reserved_cost_cny": (
                    self._snapshot.reserved_cost_cny - active.reserved_cost_cny
                ),
                "soft_cap_reached": (
                    self._snapshot.soft_cap_reached
                    or committed >= self._limits.soft_cost_cny
                ),
            }
        )
        return self._snapshot

    def fail_model_call(self, reservation: ModelReservation) -> GovernanceSnapshot:
        active = self._require_active(reservation)
        del self._reservations[active.reservation_id]
        committed = self._snapshot.committed_cost_cny + active.reserved_cost_cny
        self._snapshot = self._snapshot.model_copy(
            update={
                "committed_cost_cny": committed,
                "reserved_cost_cny": (
                    self._snapshot.reserved_cost_cny - active.reserved_cost_cny
                ),
                "soft_cap_reached": (
                    self._snapshot.soft_cap_reached
                    or committed >= self._limits.soft_cost_cny
                ),
            }
        )
        return self._snapshot

    def consume_tool(self, action_type: ActionType) -> GovernanceSnapshot:
        self.ensure_time_remaining()
        if self._snapshot.tool_calls >= self._limits.max_tool_calls:
            raise BudgetExceeded(StopReason.TOOL_CALL_LIMIT)
        if (
            action_type is ActionType.EXECUTE_SQL
            and self._snapshot.execute_calls >= self._limits.max_execute_calls
        ):
            raise BudgetExceeded(StopReason.EXECUTE_LIMIT)
        if (
            action_type is ActionType.PROFILE
            and self._snapshot.profile_calls >= self._limits.max_profile_calls
        ):
            raise BudgetExceeded(StopReason.PROFILE_LIMIT)
        updates = {"tool_calls": self._snapshot.tool_calls + 1}
        if action_type is ActionType.EXECUTE_SQL:
            updates["execute_calls"] = self._snapshot.execute_calls + 1
        if action_type is ActionType.PROFILE:
            updates["profile_calls"] = self._snapshot.profile_calls + 1
        self._snapshot = self._snapshot.model_copy(update=updates)
        return self._snapshot

    def ensure_action_loop_available(self) -> None:
        self.ensure_time_remaining()
        if self._snapshot.action_loops >= self._limits.max_action_loops:
            raise BudgetExceeded(StopReason.ANALYSIS_LOOP_LIMIT)

    def consume_action_loop(self) -> GovernanceSnapshot:
        if self._snapshot.action_loops >= self._limits.max_action_loops:
            raise BudgetExceeded(StopReason.ANALYSIS_LOOP_LIMIT)
        self._snapshot = self._snapshot.model_copy(
            update={"action_loops": self._snapshot.action_loops + 1}
        )
        return self._snapshot

    def consume_repair(self) -> GovernanceSnapshot:
        self.ensure_time_remaining()
        if self._snapshot.repair_count >= self._limits.max_repairs:
            raise BudgetExceeded(StopReason.REPAIR_FAILED)
        self._snapshot = self._snapshot.model_copy(
            update={"repair_count": self._snapshot.repair_count + 1}
        )
        return self._snapshot

    def ensure_time_remaining(self) -> None:
        if self._monotonic() >= self._snapshot.deadline_monotonic:
            raise BudgetExceeded(StopReason.TASK_TIMEOUT)

    def _require_active(self, reservation: ModelReservation) -> ModelReservation:
        active = self._reservations.get(reservation.reservation_id)
        if active is None or active != reservation:
            raise RuntimeError("reservation is unknown or already settled")
        return active


__all__ = ["BudgetExceeded", "BudgetLedger", "BudgetLimits"]
