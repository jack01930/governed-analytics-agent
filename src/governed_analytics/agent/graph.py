"""Bounded LangGraph assembly and the public one-run execution seam."""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    SafeTrace,
    StopReason,
)
from governed_analytics.agent.nodes.behavior import decide_behavior, intake
from governed_analytics.agent.nodes.execution import (
    invoke_tool,
    judge_evidence,
    repair,
    route_action,
    validate_observation_node,
    validate_profile,
)
from governed_analytics.agent.nodes.planning import (
    build_plan,
    compile_contract,
    replan,
    retrieve_context,
)
from governed_analytics.agent.nodes.synthesis import finalize, synthesize
from governed_analytics.agent.ports import AgentContext
from governed_analytics.agent.state import AgentState, new_agent_state
from governed_analytics.agent.validation import repair_decision

type IntakeRoute = Literal["decide_behavior", "finalize"]
type BehaviorRoute = Literal["retrieve_context", "finalize"]
type ContextRoute = Literal["build_plan", "finalize"]
type PlanRoute = Literal["compile_contract", "finalize"]
type ContractRoute = Literal["route_action", "finalize"]
type ActionRoute = Literal["invoke_tool", "finalize"]
type ToolRoute = Literal["validate_profile", "validate_observation", "finalize"]
type ProfileRoute = Literal["replan", "finalize"]
type ValidationRoute = Literal["judge_evidence", "repair", "finalize"]
type EvidenceRoute = Literal["replan", "synthesize", "finalize"]
type ReplanRoute = Literal["route_action", "finalize"]
type RepairRoute = Literal["validate_observation", "finalize"]


def route_after_intake(state: AgentState) -> IntakeRoute:
    return "finalize" if state["stop_reason"] is not None else "decide_behavior"


def route_after_behavior(state: AgentState) -> BehaviorRoute:
    return "finalize" if state["stop_reason"] is not None else "retrieve_context"


def route_after_context(state: AgentState) -> ContextRoute:
    return "finalize" if state["stop_reason"] is not None else "build_plan"


def route_after_plan(state: AgentState) -> PlanRoute:
    return "finalize" if state["stop_reason"] is not None else "compile_contract"


def route_after_contract(state: AgentState) -> ContractRoute:
    if state["stop_reason"] is not None or state["answer_contract"] is None:
        return "finalize"
    return "route_action"


def route_after_action(state: AgentState) -> ActionRoute:
    return (
        "invoke_tool"
        if state["stop_reason"] is None and state["next_action"] is not None
        else "finalize"
    )


def route_after_tool(state: AgentState) -> ToolRoute:
    if state["stop_reason"] is not None:
        return "finalize"
    action = state["next_action"]
    if action is None or not state["observations"]:
        return "finalize"
    if action.action_type is ActionType.PROFILE:
        return "validate_profile"
    if action.action_type is ActionType.EXECUTE_SQL:
        return "validate_observation"
    return "finalize"


def route_after_profile(state: AgentState) -> ProfileRoute:
    return "finalize" if state["stop_reason"] is not None else "replan"


def route_after_validation(state: AgentState) -> ValidationRoute:
    if state["stop_reason"] is not None:
        return "finalize"
    if not state["observation_validations"]:
        return "finalize"
    validation = state["observation_validations"][-1]
    if validation.valid:
        return "judge_evidence"
    if state["stop_reason"] is not None:
        return "finalize"
    decision = repair_decision(
        validation,
        state["repair_history"],
        state["governance"],
    )
    return "repair" if decision.allowed else "finalize"


def route_after_evidence(state: AgentState) -> EvidenceRoute:
    if state["governance"].soft_cap_reached:
        return "finalize"
    if state["stop_reason"] in {StopReason.ANSWER_COMPLETE, StopReason.PREMISE_NOT_MET}:
        return "synthesize"
    if state["stop_reason"] is not None:
        return "finalize"
    return "replan"


def route_after_replan(state: AgentState) -> ReplanRoute:
    return "finalize" if state["stop_reason"] is not None else "route_action"


def route_after_repair(state: AgentState) -> RepairRoute:
    if state["stop_reason"] is not None or not state["observations"]:
        return "finalize"
    return "validate_observation"


def build_agent_graph() -> CompiledStateGraph[
    AgentState,
    AgentContext,
    AgentState,
    AgentState,
]:
    """Compile the bounded graph without persistence, stores, or hidden dependencies."""

    builder = StateGraph(AgentState, context_schema=AgentContext)
    builder.add_node("intake", intake)
    builder.add_node("decide_behavior", decide_behavior)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("build_plan", build_plan)
    builder.add_node("compile_contract", compile_contract)
    builder.add_node("route_action", route_action)
    builder.add_node("invoke_tool", invoke_tool)
    builder.add_node("validate_profile", validate_profile)
    builder.add_node("validate_observation", validate_observation_node)
    builder.add_node("judge_evidence", judge_evidence)
    builder.add_node("replan", replan)
    builder.add_node("repair", repair)
    builder.add_node("synthesize", synthesize)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake)
    builder.add_conditional_edges("decide_behavior", route_after_behavior)
    builder.add_conditional_edges("retrieve_context", route_after_context)
    builder.add_conditional_edges("build_plan", route_after_plan)
    builder.add_conditional_edges("compile_contract", route_after_contract)
    builder.add_conditional_edges("route_action", route_after_action)
    builder.add_conditional_edges("invoke_tool", route_after_tool)
    builder.add_conditional_edges("validate_profile", route_after_profile)
    builder.add_conditional_edges("validate_observation", route_after_validation)
    builder.add_conditional_edges("judge_evidence", route_after_evidence)
    builder.add_conditional_edges("replan", route_after_replan)
    builder.add_conditional_edges("repair", route_after_repair)
    builder.add_edge("synthesize", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(name="week3-bounded-analytics-agent")


async def run_agent(
    *,
    run_id: str,
    query: str,
    context: AgentContext,
) -> AgentRunResult:
    graph = build_agent_graph()
    state: AgentState | None = None
    try:
        raw_state = await graph.ainvoke(
            new_agent_state(run_id=run_id, query=query),
            context=context,
        )
        state = cast(AgentState, raw_state)
        final_answer = state["final_answer"]
        if final_answer is None:
            raise RuntimeError("agent graph did not finalize")
        safe_trace = context.trace_recorder.snapshot()
        if (
            safe_trace.nodes != state["node_traces"]
            or safe_trace.model_calls != state["model_call_traces"]
            or safe_trace.tool_calls != state["tool_call_traces"]
        ):
            raise RuntimeError("agent trace histories diverged")
        return AgentRunResult(
            run_id=state["run_id"],
            behavior=state["behavior"],
            answer_contract=state["answer_contract"],
            observations=state["observations"],
            observation_validations=state["observation_validations"],
            evidence=state["evidence"],
            evidence_gaps=state["evidence_gaps"],
            first_candidate=state["first_candidate"],
            repair_history=state["repair_history"],
            governance=state["governance"],
            final_answer=final_answer,
            safe_trace=safe_trace,
        )
    except NodeCancelledError:
        raise asyncio.CancelledError from None
    except Exception:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError from None
        return _internal_fallback(run_id=run_id, context=context, state=state)


def _prefer_live_history[T](state_items: tuple[T, ...], live_items: tuple[T, ...]) -> tuple[T, ...]:
    common = min(len(state_items), len(live_items))
    if state_items[:common] == live_items[:common]:
        return state_items if len(state_items) > len(live_items) else live_items
    return live_items


def _internal_fallback(
    *,
    run_id: str,
    context: AgentContext,
    state: AgentState | None,
) -> AgentRunResult:
    state_trace = (
        SafeTrace(
            nodes=state["node_traces"],
            model_calls=state["model_call_traces"],
            tool_calls=state["tool_call_traces"],
        )
        if state is not None
        else SafeTrace()
    )
    try:
        live_trace = context.trace_recorder.snapshot()
    except Exception:
        fallback_trace = state_trace
    else:
        fallback_trace = SafeTrace(
            nodes=_prefer_live_history(state_trace.nodes, live_trace.nodes),
            model_calls=_prefer_live_history(state_trace.model_calls, live_trace.model_calls),
            tool_calls=_prefer_live_history(state_trace.tool_calls, live_trace.tool_calls),
        )
    try:
        governance = context.budget.snapshot
    except Exception:
        governance = state["governance"] if state is not None else GovernanceSnapshot()
    answer = FinalAnswer(
        status=FinalStatus.INTERNAL_ERROR,
        stop_reason=StopReason.INTERNAL_ERROR,
        answer="分析因内部受控错误终止。",
    )
    return AgentRunResult(
        run_id=run_id,
        behavior=None,
        answer_contract=None,
        observations=(),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=None,
        repair_history=(),
        governance=governance,
        final_answer=answer,
        safe_trace=fallback_trace,
    )


__all__ = ["build_agent_graph", "run_agent"]
