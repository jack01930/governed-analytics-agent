"""LangGraph-compatible append-only state for one governed agent run."""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from governed_analytics.agent.contracts import (
    AgentAction,
    AnswerContract,
    BehaviorDecision,
    EvidenceItem,
    FinalAnswer,
    GovernanceSnapshot,
    ModelCallTrace,
    NodeTrace,
    Observation,
    ObservationValidation,
    RepairRecord,
    RunLifecycleStatus,
    StopReason,
    ToolCallTrace,
    TypedMetricPlan,
)
from governed_analytics.tools.contracts import MetricInfo, TableInfo


class AgentState(TypedDict):
    run_id: str
    query: str
    normalized_query: str
    lifecycle_status: RunLifecycleStatus
    behavior: BehaviorDecision | None
    metric_context: tuple[MetricInfo, ...]
    schema_context: tuple[TableInfo, ...]
    plan_revisions: Annotated[tuple[TypedMetricPlan, ...], add]
    answer_contract: AnswerContract | None
    next_action: AgentAction | None
    action_loop_pending: bool
    observations: Annotated[tuple[Observation, ...], add]
    observation_validations: Annotated[tuple[ObservationValidation, ...], add]
    evidence: Annotated[tuple[EvidenceItem, ...], add]
    evidence_gaps: tuple[str, ...]
    first_candidate: Observation | None
    repair_history: Annotated[tuple[RepairRecord, ...], add]
    governance: GovernanceSnapshot
    node_traces: Annotated[tuple[NodeTrace, ...], add]
    model_call_traces: Annotated[tuple[ModelCallTrace, ...], add]
    tool_call_traces: Annotated[tuple[ToolCallTrace, ...], add]
    final_answer: FinalAnswer | None
    stop_reason: StopReason | None


def append_observations[T](left: tuple[T, ...], right: tuple[T, ...]) -> tuple[T, ...]:
    """Append reducer preserving the complete immutable history."""
    return left + right


def new_agent_state(*, run_id: str, query: str) -> AgentState:
    """Create a fully initialized non-terminal state with no hidden evaluation data."""
    return AgentState(
        run_id=run_id,
        query=query,
        normalized_query=query.strip(),
        lifecycle_status=RunLifecycleStatus.QUEUED,
        behavior=None,
        metric_context=(),
        schema_context=(),
        plan_revisions=(),
        answer_contract=None,
        next_action=None,
        action_loop_pending=False,
        observations=(),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=None,
        repair_history=(),
        governance=GovernanceSnapshot(),
        node_traces=(),
        model_call_traces=(),
        tool_call_traces=(),
        final_answer=None,
        stop_reason=None,
    )


__all__ = ["AgentState", "append_observations", "new_agent_state"]
