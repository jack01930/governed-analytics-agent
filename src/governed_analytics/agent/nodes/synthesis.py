"""Evidence-bound synthesis and deterministic terminal fallback."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from langgraph.runtime import Runtime

from governed_analytics.agent.contracts import (
    BehaviorAction,
    EvidenceAssessment,
    FinalAnswer,
    FinalStatus,
    JsonValue,
    RunLifecycleStatus,
    StopReason,
    StructuredModelRequest,
)
from governed_analytics.agent.nodes.behavior import (
    cast_stop_reason,
    emit_budget_warning,
    failure_delta,
    finish_node,
)
from governed_analytics.agent.ports import AgentContext, StructuredInvocationError
from governed_analytics.agent.state import AgentState
from governed_analytics.agent.validation import assess_evidence
from governed_analytics.runtime.budgets import BudgetExceeded

_SENSITIVE_TOKENS = frozenset(
    {
        "apikey",
        "alter",
        "arguments",
        "authorization",
        "bearer",
        "create",
        "delete",
        "drop",
        "endpoint",
        "filter",
        "filters",
        "insert",
        "password",
        "payload",
        "prompt",
        "raw",
        "row",
        "rows",
        "secret",
        "select",
        "sql",
        "token",
        "truncate",
        "update",
        "url",
    }
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![A-Za-z0-9_.])")


def _assessment(state: AgentState) -> EvidenceAssessment | None:
    if not state["plan_revisions"]:
        return None
    return assess_evidence(state["plan_revisions"][-1], state["evidence"])


def _safe_evidence_payload(state: AgentState) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(
        {
            "evidence_id": item.evidence_id,
            "hypothesis_id": item.hypothesis_id,
            "contract_id": item.contract_id,
            "query_id": item.query_id,
            "claim_key": item.claim_key,
            "dimensions": item.dimensions,
            "stance": item.stance,
            "numeric_value": str(item.numeric_value) if item.numeric_value is not None else None,
            "unit": item.unit,
        }
        for item in state["evidence"]
        if item.verified
    )


def _unsafe_string(value: str) -> bool:
    lowered = value.casefold()
    tokens = tuple(re.findall(r"[a-z0-9]+", lowered))
    compact = "".join(tokens)
    return (
        any(token in _SENSITIVE_TOKENS for token in tokens)
        or "apikey" in compact
        or re.search(r"(?:sk|pk)-[a-z0-9]", lowered) is not None
        or "http://" in lowered
        or "https://" in lowered
    )


def _contains_unsafe_string(value: object) -> bool:
    if isinstance(value, str):
        return _unsafe_string(value)
    if isinstance(value, Mapping):
        return any(
            _contains_unsafe_string(key) or _contains_unsafe_string(item)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_unsafe_string(item) for item in value)
    return False


def _safe_summary_string(value: str) -> str:
    return "redacted" if _unsafe_string(value) else value


def _result_summary(state: AgentState) -> Mapping[str, JsonValue]:
    return {
        "evidence": tuple(
            {
                "evidence_id": item.evidence_id,
                "claim_key": _safe_summary_string(item.claim_key),
                "numeric_value": (
                    str(item.numeric_value) if item.numeric_value is not None else None
                ),
                "unit": (_safe_summary_string(item.unit) if item.unit is not None else None),
                "dimensions": tuple(
                    {
                        "name": _safe_summary_string(name),
                        "value": _safe_summary_string(value),
                    }
                    for name, value in item.dimensions
                ),
            }
            for item in state["evidence"]
            if item.verified
        )
    }


def _answer_numbers_are_evidenced(state: AgentState, answer: FinalAnswer) -> bool:
    referenced = {
        item.evidence_id: item
        for item in state["evidence"]
        if item.verified and item.evidence_id in set(answer.evidence_ids)
    }
    allowed = {item.numeric_value for item in referenced.values() if item.numeric_value is not None}
    text = " ".join((answer.answer, *answer.limitations))
    for token in _NUMBER.findall(text):
        try:
            value = Decimal(token)
        except InvalidOperation:
            return False
        if value not in allowed:
            return False
    return True


def _valid_synthesis(state: AgentState, answer: FinalAnswer) -> bool:
    if _contains_unsafe_string((answer.answer, answer.limitations, answer.result_summary)):
        return False
    verified = tuple(item.evidence_id for item in state["evidence"] if item.verified)
    if set(answer.evidence_ids) != set(verified) or len(answer.evidence_ids) != len(verified):
        return False
    assessment = _assessment(state)
    if assessment is None:
        return False
    if (
        answer.completed_dimensions != assessment.resolved_hypotheses
        or answer.missing_dimensions != assessment.gaps
        or not _answer_numbers_are_evidenced(state, answer)
        or (answer.result_summary is not None and answer.result_summary != _result_summary(state))
    ):
        return False
    if assessment.complete:
        return (
            answer.status is FinalStatus.COMPLETED
            and answer.stop_reason is StopReason.ANSWER_COMPLETE
        )
    if assessment.premise_not_met:
        return (
            answer.status is FinalStatus.COMPLETED
            and answer.stop_reason is StopReason.PREMISE_NOT_MET
        )
    return answer.status is FinalStatus.PARTIAL


async def synthesize(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    invocation = None
    try:
        request = StructuredModelRequest.for_output(
            purpose="synthesis",
            system_prompt=(
                "Synthesize only from verified evidence IDs and return the bound final answer."
            ),
            user_payload={
                "query": state["normalized_query"],
                "evidence": _safe_evidence_payload(state),
                "evidence_gaps": state["evidence_gaps"],
            },
            output_type=FinalAnswer,
            max_output_tokens=900,
        )
        invocation = await context.model_invoker.invoke(request, FinalAnswer)
        answer = invocation.result.output
        valid = _valid_synthesis(state, answer)
        if valid:
            answer = answer.model_copy(
                update={
                    "evidence_ids": tuple(
                        item.evidence_id for item in state["evidence"] if item.verified
                    ),
                    "result_summary": _result_summary(state),
                }
            )
        delta: dict[str, object] = {
            "governance": invocation.governance,
            "model_call_traces": invocation.traces,
        }
        if invocation.repair_record is not None:
            delta["repair_history"] = (invocation.repair_record,)
        if valid:
            delta["final_answer"] = answer
            delta["stop_reason"] = answer.stop_reason
        else:
            delta["final_answer"] = None
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="synthesize",
            started_at=started_at,
            outcome="completed" if valid else "failed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None and not isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = invocation.traces
            delta["governance"] = invocation.governance
            if invocation.repair_record is not None:
                delta["repair_history"] = (invocation.repair_record,)
        delta["final_answer"] = None
        if isinstance(
            error, (BudgetExceeded, StructuredInvocationError)
        ) and not await emit_budget_warning(
            context,
            node="synthesize",
            reason=cast_stop_reason(delta["stop_reason"]),
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="synthesize",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


def _status_for_reason(reason: StopReason) -> FinalStatus:
    if reason is StopReason.MISSING_REQUIRED_FIELDS:
        return FinalStatus.CLARIFICATION_REQUIRED
    if reason in {StopReason.UNSAFE_REQUEST, StopReason.SENSITIVE_DATA_REQUEST}:
        return FinalStatus.REFUSED
    if reason in {StopReason.UNSUPPORTED_ANALYSIS, StopReason.UNSUPPORTED_DATA_DOMAIN}:
        return FinalStatus.UNSUPPORTED
    if reason in {
        StopReason.COST_SOFT_CAP,
        StopReason.COST_HARD_CAP,
        StopReason.LLM_CALL_LIMIT,
        StopReason.TOOL_CALL_LIMIT,
        StopReason.EXECUTE_LIMIT,
        StopReason.PROFILE_LIMIT,
        StopReason.ANALYSIS_LOOP_LIMIT,
        StopReason.TASK_TIMEOUT,
    }:
        return FinalStatus.BUDGET_EXHAUSTED
    if reason in {StopReason.SQL_POLICY_REJECTED, StopReason.SENSITIVE_RESULT_BLOCKED}:
        return FinalStatus.POLICY_BLOCKED
    if reason is StopReason.RESULT_TRUNCATED:
        return FinalStatus.PARTIAL
    if reason is StopReason.MODEL_UNAVAILABLE:
        return FinalStatus.MODEL_UNAVAILABLE
    if reason is StopReason.INTERNAL_ERROR:
        return FinalStatus.INTERNAL_ERROR
    return FinalStatus.EXECUTION_FAILED


def _deterministic_answer(state: AgentState) -> FinalAnswer:
    verified = tuple(item for item in state["evidence"] if item.verified)
    assessment = _assessment(state)
    completed = assessment.resolved_hypotheses if assessment is not None else ()
    gaps = assessment.gaps if assessment is not None else state["evidence_gaps"]
    if state["stop_reason"] is StopReason.RESULT_TRUNCATED:
        return FinalAnswer(
            status=FinalStatus.PARTIAL,
            stop_reason=StopReason.RESULT_TRUNCATED,
            answer="结果达到受治理行数边界, 当前证据可能不完整。",
            evidence_ids=tuple(item.evidence_id for item in verified),
            completed_dimensions=completed,
            missing_dimensions=gaps,
            limitations=("result_truncated",),
            result_summary=_result_summary(state),
        )
    if assessment is not None and assessment.complete:
        return FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer="已基于受治理且验证通过的证据完成分析。",
            evidence_ids=tuple(item.evidence_id for item in verified),
            completed_dimensions=completed,
            result_summary=_result_summary(state),
        )
    if assessment is not None and assessment.premise_not_met:
        return FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.PREMISE_NOT_MET,
            answer="受治理证据表明分析前提不成立。",
            evidence_ids=tuple(item.evidence_id for item in verified),
            completed_dimensions=completed,
            result_summary=_result_summary(state),
        )
    if verified:
        limitation_reason = state["stop_reason"] or StopReason.EVIDENCE_PARTIAL
        return FinalAnswer(
            status=FinalStatus.PARTIAL,
            stop_reason=StopReason.EVIDENCE_PARTIAL,
            answer="已返回当前验证通过的部分证据, 分析尚未完整。",
            evidence_ids=tuple(item.evidence_id for item in verified),
            completed_dimensions=completed,
            missing_dimensions=gaps,
            limitations=(f"stopped:{limitation_reason.value}",),
            result_summary=_result_summary(state),
        )
    reason = state["stop_reason"] or StopReason.ANSWER_CONTRACT_UNMET
    behavior = state["behavior"]
    if behavior is not None and behavior.action is not BehaviorAction.EXECUTE:
        if behavior.action is BehaviorAction.CLARIFY:
            answer_text = "请补充完成分析所需的指标或时间范围。"
        elif behavior.action is BehaviorAction.REFUSE:
            answer_text = "该请求不符合受治理分析的安全要求。"
        else:
            answer_text = "当前受治理分析范围不支持该请求。"
    elif reason is StopReason.COST_HARD_CAP:
        answer_text = "预算不足, 未发起分析调用。"
    elif reason is StopReason.COST_SOFT_CAP:
        answer_text = "已达到成本软上限, 且尚无可用证据。"
    else:
        answer_text = "分析未能形成可验证答案。"
    return FinalAnswer(
        status=_status_for_reason(reason),
        stop_reason=reason,
        answer=answer_text,
        missing_dimensions=gaps,
        result_summary=_result_summary(state),
    )


async def finalize(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    answer = state["final_answer"]
    verified = {item.evidence_id for item in state["evidence"] if item.verified}
    if answer is None or not set(answer.evidence_ids).issubset(verified):
        answer = _deterministic_answer(state)
    return finish_node(
        context=context,
        node="finalize",
        started_at=started_at,
        outcome="completed",
        delta={
            "lifecycle_status": RunLifecycleStatus.TERMINAL,
            "final_answer": answer,
            "stop_reason": answer.stop_reason,
            "governance": context.budget.snapshot,
        },
    )


__all__ = ["finalize", "synthesize"]
