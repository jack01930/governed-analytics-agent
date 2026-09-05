"""Input validation, behavior routing, and shared safe node helpers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Literal

from langgraph.runtime import Runtime

from governed_analytics.agent.contracts import (
    AgentModelErrorCategory,
    BehaviorAction,
    BehaviorDecision,
    BehaviorReasonCode,
    GovernanceSnapshot,
    JsonValue,
    NodeTrace,
    StopReason,
    StructuredModelRequest,
)
from governed_analytics.agent.ports import (
    AgentContext,
    AgentModelError,
    StructuredInvocationError,
)
from governed_analytics.agent.state import AgentState
from governed_analytics.runtime.budgets import BudgetExceeded
from governed_analytics.safety.sql_policy import SqlPolicyError

MAX_QUERY_LENGTH = 4096

_BUDGET_STOP_REASONS = frozenset(
    {
        StopReason.COST_SOFT_CAP,
        StopReason.COST_HARD_CAP,
        StopReason.LLM_CALL_LIMIT,
        StopReason.TOOL_CALL_LIMIT,
        StopReason.EXECUTE_LIMIT,
        StopReason.PROFILE_LIMIT,
        StopReason.ANALYSIS_LOOP_LIMIT,
        StopReason.TASK_TIMEOUT,
    }
)
_GOVERNED_MISSING_FIELDS = frozenset(
    {
        "metric",
        "time_window",
        "previous_window",
        "current_window",
        "comparison_window",
    }
)
_SENSITIVE_IDENTIFIER = re.compile(
    r"(?:^|[_.:-])(?:api[_-]?key|apikey|secret|password|token|authorization|bearer|"
    r"prompt|payload|endpoint)(?:$|[_.:-])|^(?:sk|pk)-",
    re.IGNORECASE,
)


class SafeDependencyError(RuntimeError):
    """A dependency failed after raw details were intentionally discarded."""


def propagate_cancellation() -> None:
    """Do not let cleanup failures replace an already-requested task cancellation."""

    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError from None


def map_agent_failure(error: Exception) -> StopReason:
    """Reduce every graph-visible failure to the stable public reason set."""

    if isinstance(error, BudgetExceeded):
        return error.reason
    if isinstance(error, SqlPolicyError):
        return StopReason.SQL_POLICY_REJECTED
    if isinstance(error, StructuredInvocationError):
        return error.stop_reason
    if isinstance(error, AgentModelError):
        if error.category in {
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
        if error.category in {
            AgentModelErrorCategory.INVALID_JSON,
            AgentModelErrorCategory.INVALID_STRUCTURE,
            AgentModelErrorCategory.SCHEMA_IDENTITY_MISMATCH,
        }:
            return StopReason.STRUCTURED_OUTPUT_INVALID
    return StopReason.INTERNAL_ERROR


def safe_error_stop_reason(safe_error: str | None) -> StopReason:
    mapping = {
        "database_error": StopReason.DATABASE_ERROR,
        "sql_timeout": StopReason.SQL_TIMEOUT,
        "sensitive_output": StopReason.SENSITIVE_RESULT_BLOCKED,
        "malformed_sql": StopReason.SQL_POLICY_REJECTED,
        "read_only_policy": StopReason.SQL_POLICY_REJECTED,
        "forbidden_relation": StopReason.SQL_POLICY_REJECTED,
        "forbidden_function": StopReason.SQL_POLICY_REJECTED,
        "nondeterministic_query": StopReason.SQL_POLICY_REJECTED,
        "tool_not_allowed_in_node": StopReason.PLAN_INVALID,
        "output_shape_policy": StopReason.SQL_POLICY_REJECTED,
        "invalid_request": StopReason.PLAN_INVALID,
        "not_found": StopReason.PLAN_INVALID,
    }
    return mapping.get(safe_error or "", StopReason.INTERNAL_ERROR)


def failure_delta(error: Exception, context: AgentContext) -> dict[str, object]:
    """Preserve governed accounting metadata while discarding the raw exception."""

    propagate_cancellation()
    delta: dict[str, object] = {
        "stop_reason": map_agent_failure(error),
        "governance": context.budget.snapshot,
    }
    if isinstance(error, StructuredInvocationError):
        delta["model_call_traces"] = error.traces
        delta["governance"] = error.governance
        if error.repair_record is not None:
            delta["repair_history"] = (error.repair_record,)
    return delta


def finish_node(
    *,
    context: AgentContext,
    node: str,
    started_at: float,
    outcome: Literal["completed", "failed", "skipped"],
    delta: dict[str, object],
    consume_action_loop_on_failure: bool = False,
) -> dict[str, object]:
    trace = NodeTrace(
        node=node,
        duration_ms=max(0, round((context.clock.monotonic() - started_at) * 1000)),
        outcome=outcome,
    )
    try:
        context.trace_recorder.append_node(trace)
    except Exception:
        propagate_cancellation()
        delta.pop("node_traces", None)
        delta["stop_reason"] = StopReason.INTERNAL_ERROR
        delta["final_answer"] = None
        delta["next_action"] = None
        delta["action_loop_pending"] = False
        if consume_action_loop_on_failure:
            try:
                delta["governance"] = context.budget.consume_action_loop()
            except Exception:
                delta["governance"] = context.budget.snapshot
        else:
            delta["governance"] = context.budget.snapshot
        return delta
    delta["node_traces"] = (trace,)
    return delta


async def emit_domain_event(
    context: AgentContext,
    *,
    node: str,
    event_type: str,
    data: Mapping[str, JsonValue],
) -> bool:
    """Emit one safe domain event without exposing sink failures."""

    try:
        await context.events.emit(node, event_type, data)
    except Exception:
        propagate_cancellation()
        return False
    return True


def sensitive_identifier(value: str) -> bool:
    return _SENSITIVE_IDENTIFIER.search(value) is not None


def behavior_event_data(decision: BehaviorDecision) -> Mapping[str, JsonValue]:
    return {
        "action": decision.action.value,
        "reason_code": decision.reason_code.value,
        "missing_fields": decision.missing_fields,
    }


def budget_warning_data(
    reason: StopReason,
    governance: GovernanceSnapshot,
) -> Mapping[str, JsonValue]:
    return {
        "reason": reason.value,
        "llm_calls": governance.llm_calls,
        "tool_calls": governance.tool_calls,
        "execute_calls": governance.execute_calls,
        "profile_calls": governance.profile_calls,
        "repair_count": governance.repair_count,
        "committed_cost_cny": str(governance.committed_cost_cny),
    }


async def emit_budget_warning(
    context: AgentContext,
    *,
    node: str,
    reason: StopReason,
    force: bool = False,
) -> bool:
    if not force and reason not in _BUDGET_STOP_REASONS:
        return True
    return await emit_domain_event(
        context,
        node=node,
        event_type="budget.warning",
        data=budget_warning_data(reason, context.budget.snapshot),
    )


def behavior_stop_reason(decision: BehaviorDecision) -> StopReason | None:
    if decision.action is BehaviorAction.EXECUTE:
        return None
    if decision.action is BehaviorAction.CLARIFY:
        return StopReason.MISSING_REQUIRED_FIELDS
    if decision.reason_code is BehaviorReasonCode.UNSAFE_REQUEST:
        return StopReason.UNSAFE_REQUEST
    if decision.reason_code is BehaviorReasonCode.SENSITIVE_DATA_REQUEST:
        return StopReason.SENSITIVE_DATA_REQUEST
    if decision.reason_code is BehaviorReasonCode.UNSUPPORTED_DATA_DOMAIN:
        return StopReason.UNSUPPORTED_DATA_DOMAIN
    return StopReason.UNSUPPORTED_ANALYSIS


async def intake(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    normalized = state["query"].strip()
    delta: dict[str, object] = {
        "normalized_query": normalized,
        "lifecycle_status": "running",
    }
    if not normalized or len(normalized) > MAX_QUERY_LENGTH:
        delta["stop_reason"] = StopReason.MISSING_REQUIRED_FIELDS
    return finish_node(
        context=context,
        node="intake",
        started_at=started_at,
        outcome="completed",
        delta=delta,
    )


async def decide_behavior(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    invocation = None
    try:
        request = StructuredModelRequest.for_output(
            purpose="behavior",
            system_prompt=(
                "Classify the governed analytics request. Return only the bound schema."
            ),
            user_payload={"query": state["normalized_query"]},
            output_type=BehaviorDecision,
            max_output_tokens=300,
        )
        invocation = await context.model_invoker.invoke(request, BehaviorDecision)
        decision = invocation.result.output
        if not set(decision.missing_fields).issubset(_GOVERNED_MISSING_FIELDS):
            invalid_delta: dict[str, object] = {
                "stop_reason": StopReason.STRUCTURED_OUTPUT_INVALID,
                "governance": invocation.governance,
                "model_call_traces": invocation.traces,
            }
            if invocation.repair_record is not None:
                invalid_delta["repair_history"] = (invocation.repair_record,)
            return finish_node(
                context=context,
                node="decide_behavior",
                started_at=started_at,
                outcome="failed",
                delta=invalid_delta,
            )
        if not await emit_domain_event(
            context,
            node="decide_behavior",
            event_type="behavior.decided",
            data=behavior_event_data(decision),
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "behavior": decision,
            "stop_reason": behavior_stop_reason(decision),
            "governance": invocation.governance,
            "model_call_traces": invocation.traces,
        }
        if invocation.repair_record is not None:
            delta["repair_history"] = (invocation.repair_record,)
        if invocation.governance.soft_cap_reached:
            if not await emit_budget_warning(
                context,
                node="decide_behavior",
                reason=StopReason.COST_SOFT_CAP,
            ):
                raise SafeDependencyError("event_sink_failed")
            if decision.action is BehaviorAction.EXECUTE:
                delta["stop_reason"] = StopReason.COST_SOFT_CAP
        return finish_node(
            context=context,
            node="decide_behavior",
            started_at=started_at,
            outcome="completed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None and not isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = invocation.traces
            delta["governance"] = invocation.governance
            if invocation.repair_record is not None:
                delta["repair_history"] = (invocation.repair_record,)
        if isinstance(
            error, (BudgetExceeded, StructuredInvocationError)
        ) and not await emit_budget_warning(
            context,
            node="decide_behavior",
            reason=cast_stop_reason(delta["stop_reason"]),
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="decide_behavior",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


def cast_stop_reason(value: object) -> StopReason:
    return value if isinstance(value, StopReason) else StopReason.INTERNAL_ERROR


__all__ = [
    "SafeDependencyError",
    "budget_warning_data",
    "cast_stop_reason",
    "decide_behavior",
    "emit_budget_warning",
    "emit_domain_event",
    "failure_delta",
    "finish_node",
    "intake",
    "map_agent_failure",
    "propagate_cancellation",
    "safe_error_stop_reason",
    "sensitive_identifier",
]
