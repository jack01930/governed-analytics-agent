"""Bounded action routing, tool execution, validation, evidence, and result repair."""

from __future__ import annotations

import re
from collections.abc import Mapping
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
    TypedMetricPlan,
)
from governed_analytics.agent.nodes.behavior import (
    cast_stop_reason,
    emit_budget_warning,
    failure_delta,
    finish_node,
    safe_error_stop_reason,
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
    observation: Observation,
) -> Mapping[str, JsonValue]:
    return {
        "tool_name": observation.tool_name.value,
        "purpose": observation.purpose,
        "query_id": observation.query_id,
        "columns": _safe_event_columns(state, observation),
        "row_count": observation.row_count,
        "possibly_truncated": observation.possibly_truncated,
    }


def tool_failed_data(observation: Observation) -> Mapping[str, JsonValue]:
    return {
        "tool_name": observation.tool_name.value,
        "purpose": observation.purpose,
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
    if action.hypothesis_id != pending:
        return False
    if action.action_type is ActionType.PROFILE:
        return action.contract_id is None
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
            await emit_budget_warning(
                context,
                node="route_action",
                reason=StopReason.COST_SOFT_CAP,
            )
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
            await emit_budget_warning(
                context,
                node="route_action",
                reason=StopReason.COST_SOFT_CAP,
            )
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
        if isinstance(error, (BudgetExceeded, StructuredInvocationError)):
            await emit_budget_warning(
                context,
                node="route_action",
                reason=cast_stop_reason(delta["stop_reason"]),
            )
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
    observation: Observation,
) -> None:
    if observation.ok:
        await context.events.emit(
            node,
            "tool.completed",
            tool_completed_data(state, observation),
        )
    else:
        await context.events.emit(node, "tool.failed", tool_failed_data(observation))


async def invoke_tool(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    invocation = None
    try:
        action = state["next_action"]
        if action is None:
            raise ValueError("missing next action")
        if action.action_type is ActionType.PROFILE and context.budget.snapshot.soft_cap_reached:
            raise BudgetExceeded(StopReason.COST_SOFT_CAP)
        context.budget.consume_tool(action.action_type)
        await context.events.emit(
            "invoke_tool",
            "tool.started",
            tool_started_data(action),
        )
        invocation = await context.tools.invoke(action, node="invoke_tool")
        context.trace_recorder.append_tool(invocation.trace)
        await _emit_tool_result(
            context,
            node="invoke_tool",
            state=state,
            observation=invocation.observation,
        )
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
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None:
            delta["observations"] = (invocation.observation,)
            delta["tool_call_traces"] = (invocation.trace,)
            if (
                invocation.observation.tool_name is ActionType.EXECUTE_SQL
                and state["first_candidate"] is None
            ):
                delta["first_candidate"] = invocation.observation
        if isinstance(error, BudgetExceeded):
            await emit_budget_warning(
                context,
                node="invoke_tool",
                reason=error.reason,
            )
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
    try:
        action = state["next_action"]
        observation = state["observations"][-1]
        if action is None or action.action_type is not ActionType.EXECUTE_SQL:
            raise ValueError("missing execute action")
        contract_id = action.contract_id or "execute_sql"
        if not observation.ok:
            failed_delta = {
                "stop_reason": safe_error_stop_reason(observation.safe_error),
                **_consume_pending_loop(state, context),
            }
            await context.events.emit(
                "validate_observation",
                "observation.validated",
                observation_event_data(
                    contract_id=contract_id,
                    valid=False,
                    error_code=observation.safe_error,
                    repairable=False,
                ),
            )
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
        await context.events.emit(
            "validate_observation",
            "observation.validated",
            observation_event_data(
                contract_id=validation.contract_id,
                valid=validation.valid,
                error_code=validation.error_code,
                repairable=validation.repairable,
            ),
        )
        delta: dict[str, object] = {
            "observation_validations": (validation,),
            "stop_reason": None,
        }
        if not validation.valid:
            delta.update(_consume_pending_loop(state, context))
            decision = repair_decision(
                validation,
                state["repair_history"],
                context.budget.snapshot,
            )
            if context.budget.snapshot.soft_cap_reached:
                await emit_budget_warning(
                    context,
                    node="validate_observation",
                    reason=StopReason.COST_SOFT_CAP,
                )
                delta["stop_reason"] = StopReason.COST_SOFT_CAP
            elif not decision.allowed:
                delta["stop_reason"] = decision.stop_reason
        return finish_node(
            context=context,
            node="validate_observation",
            started_at=started_at,
            outcome="completed" if validation.valid else "failed",
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
        await context.events.emit(
            "judge_evidence",
            "hypothesis.updated",
            hypothesis_event_data(contract.hypothesis_id, stance),
        )
        await context.events.emit(
            "judge_evidence",
            "evidence.assessed",
            evidence_event_data(assessment),
        )
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
            await emit_budget_warning(
                context,
                node="judge_evidence",
                reason=StopReason.COST_SOFT_CAP,
            )
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
    try:
        if context.budget.snapshot.soft_cap_reached:
            raise BudgetExceeded(StopReason.COST_SOFT_CAP)
        governance = context.budget.consume_repair()
        await context.events.emit(
            "repair",
            "repair.started",
            repair_event_data(
                repair_count=governance.repair_count,
                error_code=error_code,
                success=False,
            ),
        )
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
        ):
            raise ValueError("repair action changed its governed target")
        context.budget.consume_tool(ActionType.EXECUTE_SQL)
        await context.events.emit(
            "repair",
            "tool.started",
            tool_started_data(repaired_action),
        )
        tool_invocation = await context.tools.invoke(repaired_action, node="repair")
        context.trace_recorder.append_tool(tool_invocation.trace)
        await _emit_tool_result(
            context,
            node="repair",
            state=state,
            observation=tool_invocation.observation,
        )
        success = tool_invocation.observation.ok
        record = _result_repair_record(
            original=original,
            action=action,
            error_code=error_code,
            outcome="success" if success else "failed",
            repaired=tool_invocation.observation if success else None,
        )
        await context.events.emit(
            "repair",
            "repair.completed",
            repair_event_data(
                repair_count=context.budget.snapshot.repair_count,
                error_code=error_code,
                success=success,
            ),
        )
        delta: dict[str, object] = {
            "next_action": repaired_action,
            "observations": (tool_invocation.observation,),
            "tool_call_traces": (tool_invocation.trace,),
            "model_call_traces": invocation.traces,
            "repair_history": (record,),
            "governance": context.budget.snapshot,
            "stop_reason": None
            if success
            else safe_error_stop_reason(tool_invocation.observation.safe_error),
        }
        return finish_node(
            context=context,
            node="repair",
            started_at=started_at,
            outcome="completed" if success else "failed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None and not isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = invocation.traces
            delta["governance"] = context.budget.snapshot
        if tool_invocation is not None:
            delta["observations"] = (tool_invocation.observation,)
            delta["tool_call_traces"] = (tool_invocation.trace,)
        delta["stop_reason"] = (
            error.reason if isinstance(error, BudgetExceeded) else StopReason.REPAIR_FAILED
        )
        record = _result_repair_record(
            original=original,
            action=action,
            error_code=error_code,
            outcome="failed",
        )
        delta["repair_history"] = (record,)
        if isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = error.traces
            delta["governance"] = error.governance
        await context.events.emit(
            "repair",
            "repair.completed",
            repair_event_data(
                repair_count=context.budget.snapshot.repair_count,
                error_code=error_code,
                success=False,
            ),
        )
        if isinstance(error, BudgetExceeded):
            await emit_budget_warning(context, node="repair", reason=error.reason)
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
