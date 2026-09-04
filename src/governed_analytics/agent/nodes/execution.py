"""Bounded action routing, tool execution, validation, evidence, and result repair."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from langgraph.runtime import Runtime

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    AnalysisAction,
    EvidenceAssessment,
    JsonValue,
    Observation,
    RepairRecord,
    StopReason,
    StructuredModelRequest,
    ToolCallTrace,
    ToolInvocation,
    TypedMetricPlan,
)
from governed_analytics.agent.nodes.behavior import (
    SafeDependencyError,
    cast_stop_reason,
    emit_budget_warning,
    emit_domain_event,
    failure_delta,
    finish_node,
    propagate_cancellation,
    safe_error_stop_reason,
    sensitive_identifier,
)
from governed_analytics.agent.ports import AgentContext, StructuredInvocationError
from governed_analytics.agent.state import AgentState
from governed_analytics.agent.validation import (
    assess_evidence,
    extract_evidence,
    repair_decision,
    validate_observation,
)
from governed_analytics.runtime.budgets import BudgetExceeded

_SAFE_COLUMN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_TOOL_ERRORS = frozenset(
    {
        "malformed_sql",
        "read_only_policy",
        "forbidden_relation",
        "forbidden_function",
        "nondeterministic_query",
        "output_shape_policy",
        "sensitive_output",
        "invalid_request",
        "not_found",
        "sql_timeout",
        "database_error",
        "tool_not_allowed_in_node",
        "internal_tool_error",
    }
)
_PROFILE_OPERATIONS = frozenset(
    {"time_range", "numeric_summary", "null_summary", "distinct_values", "top_values"}
)
_PROFILE_ARGUMENTS = frozenset(
    {
        "table_name",
        "column_name",
        "operation",
        "filters",
        "time_column",
        "start_at",
        "end_at",
        "limit",
    }
)


def tool_started_data(action: AgentAction) -> Mapping[str, JsonValue]:
    return {
        "tool_name": action.action_type.value,
        "purpose": _event_purpose(action),
        "contract_id": action.contract_id,
    }


def _event_purpose(action: AgentAction) -> str:
    if action.action_type is ActionType.EXECUTE_SQL:
        return action.contract_id or action.hypothesis_id or "execute_sql"
    return "profile_context"


def _safe_event_columns(
    state: AgentState,
    observation: Observation,
) -> tuple[str, ...]:
    if any(_SAFE_COLUMN.fullmatch(column) is None for column in observation.columns):
        return ()
    if observation.tool_name is ActionType.EXECUTE_SQL:
        contract = state["answer_contract"]
        if contract is None or observation.contract_id is None:
            return ()
        try:
            expected = contract.contract(observation.contract_id).column_names
        except KeyError:
            return ()
        if observation.columns != expected:
            return ()
    return observation.columns


def tool_completed_data(
    state: AgentState,
    action: AgentAction,
    observation: Observation,
) -> Mapping[str, JsonValue]:
    return {
        "tool_name": action.action_type.value,
        "purpose": _event_purpose(action),
        "query_id": observation.query_id,
        "columns": _safe_event_columns(state, observation),
        "row_count": observation.row_count,
        "possibly_truncated": observation.possibly_truncated,
    }


def tool_failed_data(action: AgentAction, observation: Observation) -> Mapping[str, JsonValue]:
    return {
        "tool_name": action.action_type.value,
        "purpose": _event_purpose(action),
        "safe_error": observation.safe_error,
    }


def observation_event_data(
    *,
    contract_id: str,
    valid: bool,
    error_code: str | None,
    repairable: bool,
) -> Mapping[str, JsonValue]:
    return {
        "contract_id": contract_id,
        "valid": valid,
        "error_code": error_code,
        "repairable": repairable,
    }


def hypothesis_event_data(
    hypothesis_id: str,
    status: str,
) -> Mapping[str, JsonValue]:
    return {"hypothesis_id": hypothesis_id, "status": status}


def evidence_event_data(assessment: EvidenceAssessment) -> Mapping[str, JsonValue]:
    return {
        "verified_count": assessment.verified_count,
        "gaps": assessment.gaps,
        "partial": assessment.partial,
        "complete": assessment.complete,
    }


def repair_event_data(
    *,
    repair_count: int,
    error_code: str,
    success: bool,
) -> Mapping[str, JsonValue]:
    return {
        "repair_count": repair_count,
        "error_code": error_code,
        "success": success,
    }


def _current_plan(state: AgentState) -> TypedMetricPlan:
    return state["plan_revisions"][-1]


def _pending_hypothesis(state: AgentState) -> str | None:
    return next(
        (
            item.hypothesis_id
            for item in _current_plan(state).hypotheses
            if item.status == "pending"
        ),
        None,
    )


def _action_prompt(state: AgentState, hypothesis_id: str) -> Mapping[str, object]:
    contract = state["answer_contract"]
    assert contract is not None
    matching = tuple(
        item for item in contract.observation_contracts if item.hypothesis_id == hypothesis_id
    )
    return {
        "query": state["normalized_query"],
        "target_hypothesis_id": hypothesis_id,
        "contracts": tuple(
            {
                "contract_id": item.contract_id,
                "hypothesis_id": item.hypothesis_id,
                "columns": item.column_names,
                "shape": item.shape.value,
                "limit": item.limit,
            }
            for item in matching
        ),
        "profile_observations": tuple(
            {
                "tool_name": item.tool_name.value,
                "purpose": item.purpose,
                "columns": item.columns,
                "row_count": item.row_count,
            }
            for item in state["observations"]
            if item.tool_name is ActionType.PROFILE
        ),
        "evidence_gaps": state["evidence_gaps"],
    }


def _valid_action_target(state: AgentState, action: AnalysisAction, pending: str) -> bool:
    if sensitive_identifier(action.purpose):
        return False
    if action.hypothesis_id != pending:
        return False
    if action.action_type is ActionType.PROFILE:
        metadata = _profile_safe_arguments(action)
        if action.contract_id is not None or metadata is None:
            return False
        arguments = action.arguments
        table_name = arguments.get("table_name")
        column_name = arguments.get("column_name")
        time_column = arguments.get("time_column")
        if (
            not isinstance(table_name, str)
            or not isinstance(column_name, str)
            or (time_column is not None and not isinstance(time_column, str))
        ):
            return False
        raw_filters = arguments.get("filters", ())
        identifiers = [table_name, column_name]
        if time_column is not None:
            identifiers.append(time_column)
        if not isinstance(raw_filters, tuple):
            return False
        for item in raw_filters:
            if not isinstance(item, Mapping):
                return False
            filter_column = item.get("column_name")
            if not isinstance(filter_column, str):
                return False
            identifiers.append(filter_column)
        if any(not isinstance(item, str) or sensitive_identifier(item) for item in identifiers):
            return False
        table = next(
            (item for item in state["schema_context"] if item.name == table_name),
            None,
        )
        if table is None:
            return False
        columns = {item.name: item for item in table.columns}
        if not all(item in columns for item in identifiers[1:]):
            return False
        target_column = columns[column_name]
        operation = action.arguments.get("operation", "distinct_values")
        if operation == "numeric_summary" and not target_column.data_type.startswith(
            ("bigint", "integer", "numeric")
        ):
            return False
        if operation == "time_range" and target_column.data_type != "timestamptz":
            return False
        return time_column is None or columns[time_column].data_type == "timestamptz"
    contract = state["answer_contract"]
    if contract is None or action.contract_id is None:
        return False
    try:
        target = contract.contract(action.contract_id)
    except KeyError:
        return False
    return target.hypothesis_id == pending


async def route_action(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    invocation = None
    try:
        if context.budget.snapshot.soft_cap_reached:
            if not await emit_budget_warning(
                context,
                node="route_action",
                reason=StopReason.COST_SOFT_CAP,
            ):
                raise SafeDependencyError("event_sink_failed")
            return finish_node(
                context=context,
                node="route_action",
                started_at=started_at,
                outcome="skipped",
                delta={
                    "next_action": None,
                    "stop_reason": StopReason.COST_SOFT_CAP,
                    "governance": context.budget.snapshot,
                },
            )
        if sum(trace.purpose == "action" for trace in state["model_call_traces"]) >= 4:
            raise BudgetExceeded(StopReason.ANALYSIS_LOOP_LIMIT)
        pending = _pending_hypothesis(state)
        if pending is None:
            raise ValueError("no pending hypothesis")
        request = StructuredModelRequest.for_output(
            purpose="action",
            system_prompt=(
                "Choose one bounded profile or execute_sql action for the supplied hypothesis."
            ),
            user_payload=_action_prompt(state, pending),
            output_type=AnalysisAction,
            max_output_tokens=900,
        )
        invocation = await context.model_invoker.invoke(request, AnalysisAction)
        action = invocation.result.output
        if not _valid_action_target(state, action, pending):
            delta: dict[str, object] = {
                "next_action": None,
                "stop_reason": StopReason.PLAN_INVALID,
                "governance": invocation.governance,
                "model_call_traces": invocation.traces,
            }
            if invocation.repair_record is not None:
                delta["repair_history"] = (invocation.repair_record,)
            return finish_node(
                context=context,
                node="route_action",
                started_at=started_at,
                outcome="failed",
                delta=delta,
            )
        pending_loop = False
        if action.action_type is ActionType.EXECUTE_SQL:
            context.budget.ensure_action_loop_available()
            pending_loop = True
        elif context.budget.snapshot.soft_cap_reached:
            if not await emit_budget_warning(
                context,
                node="route_action",
                reason=StopReason.COST_SOFT_CAP,
            ):
                raise SafeDependencyError("event_sink_failed")
            soft_delta: dict[str, object] = {
                "next_action": None,
                "stop_reason": StopReason.COST_SOFT_CAP,
                "governance": context.budget.snapshot,
                "model_call_traces": invocation.traces,
            }
            if invocation.repair_record is not None:
                soft_delta["repair_history"] = (invocation.repair_record,)
            return finish_node(
                context=context,
                node="route_action",
                started_at=started_at,
                outcome="skipped",
                delta=soft_delta,
            )
        delta = {
            "next_action": action,
            "action_loop_pending": pending_loop,
            "stop_reason": None,
            "governance": context.budget.snapshot,
            "model_call_traces": invocation.traces,
        }
        if invocation.repair_record is not None:
            delta["repair_history"] = (invocation.repair_record,)
        return finish_node(
            context=context,
            node="route_action",
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
        delta["next_action"] = None
        delta["action_loop_pending"] = False
        if isinstance(
            error, (BudgetExceeded, StructuredInvocationError)
        ) and not await emit_budget_warning(
            context,
            node="route_action",
            reason=cast_stop_reason(delta["stop_reason"]),
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="route_action",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


async def _emit_tool_result(
    context: AgentContext,
    *,
    node: str,
    state: AgentState,
    action: AgentAction,
    observation: Observation,
) -> bool:
    if observation.ok:
        return await emit_domain_event(
            context,
            node=node,
            event_type="tool.completed",
            data=tool_completed_data(state, action, observation),
        )
    return await emit_domain_event(
        context,
        node=node,
        event_type="tool.failed",
        data=tool_failed_data(action, observation),
    )


def _safe_failure_invocation(
    action: AgentAction,
    *,
    safe_error: str = "internal_tool_error",
) -> ToolInvocation:
    purpose = _event_purpose(action)
    safe_arguments: tuple[tuple[str, JsonValue], ...]
    if action.action_type is ActionType.EXECUTE_SQL:
        safe_arguments = tuple(
            (name, value)
            for name, value in (
                ("contract_id", action.contract_id),
                ("hypothesis_id", action.hypothesis_id),
            )
            if value is not None
        )
    elif action.action_type is ActionType.PROFILE:
        safe_arguments = _profile_safe_arguments(action) or ()
    else:
        safe_arguments = ()
    observation = Observation(
        observation_id=uuid4().hex,
        tool_name=action.action_type,
        purpose=purpose,
        ok=False,
        safe_error=safe_error,
        hypothesis_id=action.hypothesis_id,
        contract_id=action.contract_id,
    )
    trace = ToolCallTrace(
        tool_name=action.action_type,
        purpose=purpose,
        safe_arguments=safe_arguments,
        safe_error=safe_error,
    )
    return ToolInvocation(observation=observation, trace=trace)


def _profile_safe_arguments(action: AgentAction) -> tuple[tuple[str, JsonValue], ...] | None:
    arguments = action.arguments
    if not set(arguments).issubset(_PROFILE_ARGUMENTS):
        return None
    table_name = arguments.get("table_name")
    column_name = arguments.get("column_name")
    operation = arguments.get("operation", "distinct_values")
    limit = arguments.get("limit", 50)
    raw_filters = arguments.get("filters", ())
    if (
        not isinstance(table_name, str)
        or not isinstance(column_name, str)
        or operation not in _PROFILE_OPERATIONS
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 50
        or not isinstance(raw_filters, tuple)
    ):
        return None
    filter_columns: list[str] = []
    for item in raw_filters:
        if not isinstance(item, Mapping):
            return None
        filter_column = item.get("column_name")
        value = item.get("value")
        if (
            set(item) != {"column_name", "value"}
            or not isinstance(filter_column, str)
            or type(value) not in {str, int, float, bool}
            or filter_column in filter_columns
        ):
            return None
        filter_columns.append(filter_column)
    start_at = arguments.get("start_at")
    end_at = arguments.get("end_at")
    time_column = arguments.get("time_column")
    has_window = start_at is not None or end_at is not None
    if has_window:
        if (
            not isinstance(start_at, str)
            or not isinstance(end_at, str)
            or not isinstance(time_column, str)
        ):
            return None
        try:
            start = datetime.fromisoformat(start_at)
            end = datetime.fromisoformat(end_at)
        except ValueError:
            return None
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or start >= end
        ):
            return None
    elif time_column is not None:
        return None
    values: dict[str, JsonValue] = {
        "table_name": table_name,
        "column_name": column_name,
        "operation": operation,
        "filter_columns": tuple(filter_columns),
        "has_time_window": has_window,
        "limit": limit,
    }
    if isinstance(time_column, str):
        values["time_column"] = time_column
    return tuple(sorted(values.items()))


def _invocation_matches_action(
    state: AgentState,
    action: AgentAction,
    invocation: ToolInvocation,
) -> bool:
    observation = invocation.observation
    trace = invocation.trace
    if (
        observation.tool_name is not action.action_type
        or trace.tool_name is not action.action_type
        or observation.purpose != _event_purpose(action)
        or trace.purpose != _event_purpose(action)
        or observation.contract_id != action.contract_id
        or observation.hypothesis_id != action.hypothesis_id
        or observation.safe_error != trace.safe_error
        or observation.query_id != trace.query_id
        or observation.columns != trace.columns
        or observation.row_count != trace.row_count
        or observation.possibly_truncated != trace.possibly_truncated
        or (observation.safe_error is not None and observation.safe_error not in _SAFE_TOOL_ERRORS)
    ):
        return False
    if action.action_type is ActionType.EXECUTE_SQL:
        expected_arguments = tuple(
            (name, value)
            for name, value in (
                ("contract_id", action.contract_id),
                ("hypothesis_id", action.hypothesis_id),
            )
            if value is not None
        )
        if trace.safe_arguments != expected_arguments:
            return False
        if not observation.ok:
            return observation.columns == ()
        contract = state["answer_contract"]
        if contract is None or action.contract_id is None:
            return False
        try:
            expected_columns = contract.contract(action.contract_id).column_names
        except KeyError:
            return False
        return observation.columns == expected_columns and not any(
            sensitive_identifier(column) for column in observation.columns
        )
    profile_arguments = _profile_safe_arguments(action)
    if profile_arguments is None or trace.safe_arguments != profile_arguments:
        return False
    if not observation.ok:
        return True
    operation = dict(profile_arguments)["operation"]
    if not isinstance(operation, str):
        return False
    expected_columns = {
        "distinct_values": ("value",),
        "top_values": ("value", "value_count"),
        "null_summary": ("null_count", "row_count"),
        "numeric_summary": ("min_value", "max_value", "avg_value"),
        "time_range": ("min_value", "max_value"),
    }[operation]
    return observation.columns == expected_columns


async def invoke_tool(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    invocation = None
    action = state["next_action"]
    budget_consumed = False
    trace_recorded = False
    try:
        if action is None:
            raise ValueError("missing next action")
        if action.action_type is ActionType.PROFILE and context.budget.snapshot.soft_cap_reached:
            raise BudgetExceeded(StopReason.COST_SOFT_CAP)
        context.budget.consume_tool(action.action_type)
        budget_consumed = True
        if not await emit_domain_event(
            context,
            node="invoke_tool",
            event_type="tool.started",
            data=tool_started_data(action),
        ):
            raise SafeDependencyError("event_sink_failed")
        try:
            invocation = await context.tools.invoke(action, node="invoke_tool")
        except Exception:
            propagate_cancellation()
            invocation = _safe_failure_invocation(action)
        if not _invocation_matches_action(state, action, invocation):
            invocation = _safe_failure_invocation(action)
        context.trace_recorder.append_tool(invocation.trace)
        trace_recorded = True
        if not await _emit_tool_result(
            context,
            node="invoke_tool",
            state=state,
            action=action,
            observation=invocation.observation,
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "observations": (invocation.observation,),
            "tool_call_traces": (invocation.trace,),
            "governance": context.budget.snapshot,
            "stop_reason": None,
        }
        if action.action_type is ActionType.EXECUTE_SQL and state["first_candidate"] is None:
            delta["first_candidate"] = invocation.observation
        return finish_node(
            context=context,
            node="invoke_tool",
            started_at=started_at,
            outcome="completed" if invocation.observation.ok else "failed",
            delta=delta,
            consume_action_loop_on_failure=(
                budget_consumed
                and action.action_type is ActionType.EXECUTE_SQL
                and state["action_loop_pending"]
            ),
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None:
            delta["observations"] = (invocation.observation,)
            if trace_recorded:
                delta["tool_call_traces"] = (invocation.trace,)
            if (
                invocation.observation.tool_name is ActionType.EXECUTE_SQL
                and state["first_candidate"] is None
            ):
                delta["first_candidate"] = invocation.observation
        delta["action_loop_pending"] = False
        if (
            budget_consumed
            and action is not None
            and action.action_type is ActionType.EXECUTE_SQL
            and state["action_loop_pending"]
        ):
            try:
                delta["governance"] = context.budget.consume_action_loop()
            except Exception:
                delta["governance"] = context.budget.snapshot
        if isinstance(error, BudgetExceeded) and not await emit_budget_warning(
            context,
            node="invoke_tool",
            reason=error.reason,
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="invoke_tool",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


def _consume_pending_loop(state: AgentState, context: AgentContext) -> dict[str, object]:
    if not state["action_loop_pending"]:
        return {}
    governance = context.budget.consume_action_loop()
    return {"action_loop_pending": False, "governance": governance}


def _pending_result_repair(state: AgentState) -> bool:
    if (
        state["action_loop_pending"]
        or len(state["observations"]) < 2
        or not state["observation_validations"]
        or state["governance"].repair_count != len(state["repair_history"]) + 1
    ):
        return False
    previous_validation = state["observation_validations"][-1]
    previous_observation = state["observations"][-2]
    action = state["next_action"]
    return (
        not previous_validation.valid
        and previous_validation.observation_id == previous_observation.observation_id
        and action is not None
        and action.action_type is ActionType.EXECUTE_SQL
        and action.contract_id == previous_validation.contract_id
    )


async def validate_profile(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    try:
        observation = state["observations"][-1]
        valid = (
            observation.tool_name is ActionType.PROFILE
            and observation.ok
            and observation.contract_id is None
            and observation.row_count is not None
            and observation.row_count <= 50
        )
        delta: dict[str, object] = {"next_action": None}
        if not valid:
            delta["stop_reason"] = safe_error_stop_reason(observation.safe_error)
        else:
            delta["stop_reason"] = None
        return finish_node(
            context=context,
            node="validate_profile",
            started_at=started_at,
            outcome="completed" if valid else "failed",
            delta=delta,
        )
    except Exception as error:
        return finish_node(
            context=context,
            node="validate_profile",
            started_at=started_at,
            outcome="failed",
            delta=failure_delta(error, context),
        )


async def validate_observation_node(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    loop_consumed = False
    loop_delta: dict[str, object] = {}

    def consume_loop_once() -> dict[str, object]:
        nonlocal loop_consumed, loop_delta
        if loop_consumed:
            return loop_delta
        if not state["action_loop_pending"]:
            return loop_delta
        loop_consumed = True
        loop_delta = _consume_pending_loop(state, context)
        return loop_delta

    try:
        action = state["next_action"]
        observation = state["observations"][-1]
        if action is None or action.action_type is not ActionType.EXECUTE_SQL:
            raise ValueError("missing execute action")
        contract_id = action.contract_id or "execute_sql"
        if not observation.ok:
            failed_delta = {
                "stop_reason": safe_error_stop_reason(observation.safe_error),
                **consume_loop_once(),
            }
            if not await emit_domain_event(
                context,
                node="validate_observation",
                event_type="observation.validated",
                data=observation_event_data(
                    contract_id=contract_id,
                    valid=False,
                    error_code=observation.safe_error,
                    repairable=False,
                ),
            ):
                raise SafeDependencyError("event_sink_failed")
            return finish_node(
                context=context,
                node="validate_observation",
                started_at=started_at,
                outcome="failed",
                delta=failed_delta,
            )
        answer_contract = state["answer_contract"]
        if answer_contract is None or action.contract_id is None:
            raise ValueError("missing answer contract")
        contract = answer_contract.contract(action.contract_id)
        validation = validate_observation(action, observation, contract)
        pending_repair = _pending_result_repair(state)
        if not await emit_domain_event(
            context,
            node="validate_observation",
            event_type="observation.validated",
            data=observation_event_data(
                contract_id=validation.contract_id,
                valid=validation.valid,
                error_code=validation.error_code,
                repairable=validation.repairable,
            ),
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "observation_validations": (validation,),
            "stop_reason": None,
        }
        if pending_repair:
            previous = state["observations"][-2]
            previous_validation = state["observation_validations"][-1]
            record = _result_repair_record(
                original=previous,
                action=action,
                error_code=previous_validation.error_code or "answer_contract_unmet",
                outcome="success" if validation.valid else "failed",
                repaired=observation if validation.valid else None,
            )
            if not await emit_domain_event(
                context,
                node="repair",
                event_type="repair.completed",
                data=repair_event_data(
                    repair_count=context.budget.snapshot.repair_count,
                    error_code=previous_validation.error_code or "answer_contract_unmet",
                    success=validation.valid,
                ),
            ):
                raise SafeDependencyError("event_sink_failed")
            delta["repair_history"] = (record,)
            if not validation.valid:
                delta["stop_reason"] = StopReason.REPAIR_FAILED
            return finish_node(
                context=context,
                node="validate_observation",
                started_at=started_at,
                outcome="completed" if validation.valid else "failed",
                delta=delta,
            )
        if not validation.valid:
            delta.update(consume_loop_once())
            decision = repair_decision(
                validation,
                state["repair_history"],
                context.budget.snapshot,
            )
            if context.budget.snapshot.soft_cap_reached:
                if not await emit_budget_warning(
                    context,
                    node="validate_observation",
                    reason=StopReason.COST_SOFT_CAP,
                ):
                    delta["stop_reason"] = StopReason.INTERNAL_ERROR
                else:
                    delta["stop_reason"] = StopReason.COST_SOFT_CAP
            elif not decision.allowed:
                delta["stop_reason"] = decision.stop_reason
        return finish_node(
            context=context,
            node="validate_observation",
            started_at=started_at,
            outcome="completed" if validation.valid else "failed",
            delta=delta,
            consume_action_loop_on_failure=(validation.valid and state["action_loop_pending"]),
        )
    except Exception as error:
        delta = failure_delta(error, context)
        try:
            delta.update(consume_loop_once())
        except Exception:
            delta["governance"] = context.budget.snapshot
        return finish_node(
            context=context,
            node="validate_observation",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


def _updated_plan(plan: TypedMetricPlan, hypothesis_id: str, status: str) -> TypedMetricPlan:
    hypotheses = tuple(
        item.model_copy(update={"status": status}) if item.hypothesis_id == hypothesis_id else item
        for item in plan.hypotheses
    )
    return plan.model_copy(update={"revision": plan.revision + 1, "hypotheses": hypotheses})


async def judge_evidence(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    try:
        action = state["next_action"]
        observation = state["observations"][-1]
        validation = state["observation_validations"][-1]
        answer_contract = state["answer_contract"]
        if action is None or answer_contract is None or action.contract_id is None:
            raise ValueError("missing evidence inputs")
        contract = answer_contract.contract(action.contract_id)
        new_evidence = extract_evidence(action, observation, validation, contract)
        if not new_evidence:
            empty_delta = {
                "stop_reason": StopReason.ANSWER_CONTRACT_UNMET,
                **_consume_pending_loop(state, context),
            }
            return finish_node(
                context=context,
                node="judge_evidence",
                started_at=started_at,
                outcome="failed",
                delta=empty_delta,
            )
        all_evidence = state["evidence"] + new_evidence
        stance = (
            "refuted" if all(item.stance == "refutes" for item in new_evidence) else "supported"
        )
        updated_plan = _updated_plan(_current_plan(state), contract.hypothesis_id, stance)
        assessment = assess_evidence(updated_plan, all_evidence)
        if not await emit_domain_event(
            context,
            node="judge_evidence",
            event_type="hypothesis.updated",
            data=hypothesis_event_data(contract.hypothesis_id, stance),
        ):
            raise SafeDependencyError("event_sink_failed")
        if not await emit_domain_event(
            context,
            node="judge_evidence",
            event_type="evidence.assessed",
            data=evidence_event_data(assessment),
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "evidence": new_evidence,
            "evidence_gaps": assessment.gaps,
            "plan_revisions": (updated_plan,),
            "next_action": None,
            "stop_reason": (
                assessment.stop_reason
                if assessment.complete or assessment.premise_not_met
                else None
            ),
            **_consume_pending_loop(state, context),
        }
        if context.budget.snapshot.soft_cap_reached:
            if not await emit_budget_warning(
                context,
                node="judge_evidence",
                reason=StopReason.COST_SOFT_CAP,
            ):
                delta["stop_reason"] = StopReason.INTERNAL_ERROR
            delta["governance"] = context.budget.snapshot
        return finish_node(
            context=context,
            node="judge_evidence",
            started_at=started_at,
            outcome="completed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        try:
            delta.update(_consume_pending_loop(state, context))
        except Exception:
            delta["governance"] = context.budget.snapshot
        return finish_node(
            context=context,
            node="judge_evidence",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


def _result_repair_record(
    *,
    original: Observation,
    action: AgentAction,
    error_code: str,
    outcome: str,
    repaired: Observation | None = None,
) -> RepairRecord:
    return RepairRecord(
        repair_id=uuid4().hex,
        kind="result_contract",
        target=action.contract_id or "execute_sql",
        error_code=error_code,
        outcome=outcome,  # type: ignore[arg-type]
        repair_action_type=ActionType.EXECUTE_SQL,
        repair_purpose=action.contract_id or "execute_sql",
        original_observation_id=original.observation_id,
        repaired_observation_id=(
            repaired.observation_id if repaired is not None and outcome == "success" else None
        ),
    )


async def repair(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    original = state["observations"][-1]
    action = state["next_action"]
    validation = state["observation_validations"][-1]
    if action is None:
        return finish_node(
            context=context,
            node="repair",
            started_at=started_at,
            outcome="failed",
            delta={"stop_reason": StopReason.REPAIR_FAILED},
        )
    error_code = validation.error_code or "answer_contract_unmet"
    invocation = None
    tool_invocation = None
    tool_trace_recorded = False
    started_emitted = False
    completion_attempted = False
    try:
        if context.budget.snapshot.soft_cap_reached:
            raise BudgetExceeded(StopReason.COST_SOFT_CAP)
        governance = context.budget.consume_repair()
        if not await emit_domain_event(
            context,
            node="repair",
            event_type="repair.started",
            data=repair_event_data(
                repair_count=governance.repair_count,
                error_code=error_code,
                success=False,
            ),
        ):
            raise SafeDependencyError("event_sink_failed")
        started_emitted = True
        request = StructuredModelRequest.for_output(
            purpose="repair",
            system_prompt=("Repair the result contract once. Return one execute_sql action only."),
            user_payload={
                "error_code": error_code,
                "contract_id": action.contract_id,
                "hypothesis_id": action.hypothesis_id,
                "required_columns": (
                    state["answer_contract"].contract(action.contract_id).column_names
                    if state["answer_contract"] is not None and action.contract_id is not None
                    else ()
                ),
            },
            output_type=AnalysisAction,
            max_output_tokens=900,
        )
        invocation = await context.model_invoker.invoke(request, AnalysisAction)
        repaired_action = invocation.result.output
        if (
            repaired_action.action_type is not ActionType.EXECUTE_SQL
            or repaired_action.contract_id != action.contract_id
            or repaired_action.hypothesis_id != action.hypothesis_id
            or sensitive_identifier(repaired_action.purpose)
        ):
            raise ValueError("repair action changed its governed target")
        context.budget.consume_tool(ActionType.EXECUTE_SQL)
        if not await emit_domain_event(
            context,
            node="repair",
            event_type="tool.started",
            data=tool_started_data(repaired_action),
        ):
            raise SafeDependencyError("event_sink_failed")
        try:
            tool_invocation = await context.tools.invoke(repaired_action, node="repair")
        except Exception:
            propagate_cancellation()
            tool_invocation = _safe_failure_invocation(repaired_action)
        if not _invocation_matches_action(state, repaired_action, tool_invocation):
            tool_invocation = _safe_failure_invocation(repaired_action)
        context.trace_recorder.append_tool(tool_invocation.trace)
        tool_trace_recorded = True
        if not await _emit_tool_result(
            context,
            node="repair",
            state=state,
            action=repaired_action,
            observation=tool_invocation.observation,
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "next_action": repaired_action,
            "observations": (tool_invocation.observation,),
            "tool_call_traces": (tool_invocation.trace,),
            "model_call_traces": invocation.traces,
            "governance": context.budget.snapshot,
            "stop_reason": None,
        }
        if not tool_invocation.observation.ok:
            record = _result_repair_record(
                original=original,
                action=action,
                error_code=error_code,
                outcome="failed",
            )
            completion_attempted = True
            if not await emit_domain_event(
                context,
                node="repair",
                event_type="repair.completed",
                data=repair_event_data(
                    repair_count=context.budget.snapshot.repair_count,
                    error_code=error_code,
                    success=False,
                ),
            ):
                raise SafeDependencyError("event_sink_failed")
            delta["repair_history"] = (record,)
            delta["stop_reason"] = (
                StopReason.REPAIR_FAILED
                if tool_invocation.observation.safe_error == "internal_tool_error"
                else safe_error_stop_reason(tool_invocation.observation.safe_error)
            )
        return finish_node(
            context=context,
            node="repair",
            started_at=started_at,
            outcome="completed" if tool_invocation.observation.ok else "failed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None and not isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = invocation.traces
            delta["governance"] = context.budget.snapshot
        if tool_invocation is not None:
            delta["observations"] = (tool_invocation.observation,)
            if tool_trace_recorded:
                delta["tool_call_traces"] = (tool_invocation.trace,)
        delta["stop_reason"] = (
            error.reason
            if isinstance(error, BudgetExceeded)
            else StopReason.INTERNAL_ERROR
            if isinstance(error, SafeDependencyError)
            else StopReason.REPAIR_FAILED
        )
        if isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = error.traces
            delta["governance"] = error.governance
        if started_emitted and not completion_attempted:
            record = _result_repair_record(
                original=original,
                action=action,
                error_code=error_code,
                outcome="failed",
            )
            if await emit_domain_event(
                context,
                node="repair",
                event_type="repair.completed",
                data=repair_event_data(
                    repair_count=context.budget.snapshot.repair_count,
                    error_code=error_code,
                    success=False,
                ),
            ):
                delta["repair_history"] = (record,)
            else:
                delta["stop_reason"] = StopReason.INTERNAL_ERROR
        if isinstance(error, BudgetExceeded) and not await emit_budget_warning(
            context,
            node="repair",
            reason=error.reason,
            force=True,
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="repair",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


__all__ = [
    "invoke_tool",
    "judge_evidence",
    "repair",
    "route_action",
    "validate_observation_node",
    "validate_profile",
]
