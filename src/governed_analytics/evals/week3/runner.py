"""Sequential Week 3 execution seam and report derivation."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    BehaviorAction,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    SafeTrace,
    StopReason,
)
from governed_analytics.agent.graph import run_agent
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.ports import AgentContext, AgentModel, AgentTools
from governed_analytics.agent.tool_registry import SafeSqlDiagnostic
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.evals.models import QueryResult as EvalQueryResult
from governed_analytics.evals.week3.models import (
    BudgetConfiguration,
    BudgetScore,
    CandidateScore,
    FrozenExpectedResult,
    SafeEvidenceRef,
    SafeProfileTraceMetadata,
    SafeToolTraceRef,
    SuiteScore,
    Week3CaseResult,
    Week3EvaluationCase,
    Week3RunReport,
    derive_executed_manifest_sha256,
)
from governed_analytics.evals.week3.reporting import (
    cancel_week3_report_reservation,
    reserve_week3_report,
    write_week3_report,
)
from governed_analytics.evals.week3.scorers import (
    candidate_observations,
    execute_linkage_is_bijective,
    restore_query_result,
    score_behavior,
    score_candidate,
    score_evidence,
    score_tool_trace,
)
from governed_analytics.evals.week3.suites import (
    WEEK3_HELDOUT_REGISTRY,
    WEEK3_KNOWN_REGISTRY,
    load_fixture_scripts,
    load_week3_cases,
    week3_cohort_sha256,
    week3_manifest_sha256,
)
from governed_analytics.models.agent_fixtures import AgentScripts, ScriptedAgentModel
from governed_analytics.pricing import ModelPricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits


class Week3RunError(ValueError):
    """Stable global evaluation failure."""


class Week3CaseExecutor(Protocol):
    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Week3RunArtifact:
    report: Week3RunReport
    report_dir: Path
    report_json: Path
    report_markdown: Path


@dataclass(frozen=True, slots=True)
class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()


class _DiscardEvents:
    async def emit(self, node: str, event_type: str, data: Mapping[str, JsonValue]) -> None:
        del node, event_type, data


def _fixture_pricing() -> ModelPricing:
    return ModelPricing.model_validate(
        {
            "provider": "fixture",
            "region": "local",
            "requested_model": "fixture-agent",
            "resolved_model": "fixture-agent",
            "effective_date": date(2026, 9, 4),
            "currency": "CNY",
            "unit_tokens": 1,
            "input_token_upper_bound": 1_000_000,
            "input_price": "0",
            "output_price": "0",
            "pricing_basis": "deterministic fixture",
            "source": "https://example.invalid/fixture-pricing",
        }
    )


def _safe_failure(
    *,
    case_id: str,
    reason: StopReason,
    governance: GovernanceSnapshot | None = None,
    trace: SafeTrace | None = None,
) -> AgentRunResult:
    status = (
        FinalStatus.EXECUTION_FAILED
        if reason is StopReason.TASK_TIMEOUT
        else FinalStatus.INTERNAL_ERROR
    )
    return AgentRunResult(
        run_id=case_id,
        behavior=None,
        answer_contract=None,
        observations=(),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=None,
        repair_history=(),
        governance=governance or GovernanceSnapshot(),
        final_answer=FinalAnswer(
            status=status,
            stop_reason=reason,
            answer="评测用例受控终止。",
        ),
        safe_trace=trace or SafeTrace(),
    )


def _normalize_question(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


@dataclass(frozen=True, slots=True)
class _ExecutionSpec:
    script_ref: str
    max_tool_calls: int | None
    max_execute_calls: int | None


def _load_execution_specs() -> dict[str, _ExecutionSpec]:
    """Read only script refs and eval budget overrides; never load expected/Oracle data."""
    result: dict[str, _ExecutionSpec] = {}
    try:
        documents = (
            yaml.safe_load(WEEK3_KNOWN_REGISTRY.read_text(encoding="utf-8")),
            yaml.safe_load(WEEK3_HELDOUT_REGISTRY.read_text(encoding="utf-8")),
        )
        for document in documents:
            if not isinstance(document, list):
                raise ValueError
            for item in document:
                if not isinstance(item, dict):
                    raise ValueError
                case_id, script_ref = item.get("case_id"), item.get("script_ref")
                if type(case_id) is not str or type(script_ref) is not str:
                    raise ValueError
                raw_budget = item.get("budget_overrides")
                max_tools = max_execute = None
                if raw_budget is not None:
                    if not isinstance(raw_budget, dict):
                        raise ValueError
                    max_tools = raw_budget.get("max_tool_calls")
                    max_execute = raw_budget.get("max_execute_calls")
                    if max_tools is not None and type(max_tools) is not int:
                        raise ValueError
                    if max_execute is not None and type(max_execute) is not int:
                        raise ValueError
                result[case_id] = _ExecutionSpec(script_ref, max_tools, max_execute)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        raise Week3RunError("fixture execution registry is unavailable") from None
    if len(result) != 40:
        raise Week3RunError("fixture execution registry is incomplete")
    return result


def _script_library(question: str, script_ref: str) -> AgentScripts:
    scripts = {item.script_id: item for item in load_fixture_scripts()}
    try:
        script = scripts[script_ref]
    except KeyError:
        raise Week3RunError("fixture script reference is invalid") from None
    purposes: dict[str, list[object]] = {}
    for step in script.steps:
        purposes.setdefault(step.model_purpose, []).append(dict(step.output))
    return cast(AgentScripts, {_normalize_question(question): purposes})


class _BaseWeek3Executor:
    def __init__(
        self,
        *,
        tools: AgentTools,
        settings: AgentRuntimeSettings,
        pricing: ModelPricing,
    ) -> None:
        self.tools = tools
        self.settings = settings
        self.pricing = pricing
        self._clock = _Clock()

    async def _execute(
        self,
        *,
        case_id: str,
        question: str,
        model: AgentModel,
        limits: BudgetLimits,
    ) -> AgentRunResult:
        recorder = InMemoryTraceRecorder()
        budget = BudgetLedger(limits=limits, pricing=self.pricing, monotonic=self._clock.monotonic)
        context = AgentContext(
            model_invoker=StructuredModelInvoker(model, budget, recorder, self._clock),
            tools=self.tools,
            budget=budget,
            events=_DiscardEvents(),
            trace_recorder=recorder,
            clock=self._clock,
        )
        timeout = asyncio.timeout(self.settings.timeout_seconds)
        try:
            async with timeout:
                return await run_agent(run_id=case_id, query=question, context=context)
        except TimeoutError:
            if not timeout.expired():
                raise
            try:
                trace = recorder.snapshot()
            except Exception:
                trace = SafeTrace()
            try:
                governance = budget.snapshot
            except Exception:
                governance = GovernanceSnapshot()
            return _safe_failure(
                case_id=case_id,
                reason=StopReason.TASK_TIMEOUT,
                governance=governance,
                trace=trace,
            )


class FixtureWeek3CaseExecutor(_BaseWeek3Executor):
    """Fresh scripted model/budget/trace context per case over caller-owned tools."""

    def __init__(self, *, tools: AgentTools, settings: AgentRuntimeSettings) -> None:
        super().__init__(tools=tools, settings=settings, pricing=_fixture_pricing())
        self._specs = _load_execution_specs()

    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        try:
            spec = self._specs[case_id]
        except KeyError:
            raise Week3RunError("fixture case is unknown") from None
        limits = BudgetLimits.from_settings(self.settings)
        if spec.max_tool_calls is not None:
            limits = replace(limits, max_tool_calls=spec.max_tool_calls)
        if spec.max_execute_calls is not None:
            limits = replace(limits, max_execute_calls=spec.max_execute_calls)
        model = ScriptedAgentModel(_script_library(question, spec.script_ref))
        return await self._execute(case_id=case_id, question=question, model=model, limits=limits)


class LiveWeek3CaseExecutor(_BaseWeek3Executor):
    """Live executor with no imports from fixture, expected, or Oracle data at runtime."""

    def __init__(
        self,
        *,
        model: AgentModel,
        tools: AgentTools,
        settings: AgentRuntimeSettings,
        pricing: ModelPricing,
    ) -> None:
        if model.model != pricing.requested_model:
            raise Week3RunError("live requested model does not match pricing")
        super().__init__(tools=tools, settings=settings, pricing=pricing)
        self._model = model

    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        result = await self._execute(
            case_id=case_id,
            question=question,
            model=self._model,
            limits=BudgetLimits.from_settings(self.settings),
        )
        model_calls = result.safe_trace.model_calls
        if (
            not model_calls
            or any(item.provider_model != self.pricing.resolved_model for item in model_calls)
            or sum(item.input_tokens for item in model_calls) != result.governance.input_tokens
            or sum(item.output_tokens for item in model_calls) != result.governance.output_tokens
        ):
            return _safe_failure(
                case_id=case_id,
                reason=StopReason.INTERNAL_ERROR,
                governance=result.governance,
                trace=result.safe_trace,
            )
        return result


def _read_expected(path: Path) -> EvalQueryResult:
    try:
        return FrozenExpectedResult.model_validate_json(
            path.read_text(encoding="utf-8"), strict=True
        ).result
    except (OSError, TypeError, ValueError):
        raise Week3RunError("frozen expected result is unavailable") from None


def _combine(scores: tuple[CandidateScore, ...]) -> CandidateScore | None:
    if not scores:
        return None
    result_score = min(item.result_score for item in scores)
    output = all(item.output_contract_conformant for item in scores)
    validated = all(item.answer_contract_validated for item in scores)
    executed = all(item.execution_succeeded for item in scores)
    truncated = any(item.possibly_truncated for item in scores)
    return CandidateScore(
        result_score=result_score,
        output_contract_conformant=output,
        answer_contract_validated=validated,
        execution_succeeded=executed,
        possibly_truncated=truncated,
        strict_pass=result_score == 1 and output and validated and executed and not truncated,
    )


def _safe_tool_refs(result: AgentRunResult) -> tuple[SafeToolTraceRef, ...]:
    refs = []
    for trace in result.safe_trace.tool_calls:
        arguments = dict(trace.safe_arguments)
        contract_id = cast(str | None, arguments.get("contract_id"))
        hypothesis_id = cast(str | None, arguments.get("hypothesis_id"))
        purpose = {
            ActionType.METRIC_LOOKUP: ActionType.METRIC_LOOKUP.value,
            ActionType.SCHEMA_LOOKUP: ActionType.SCHEMA_LOOKUP.value,
            ActionType.PROFILE: "profile_context",
            ActionType.EXECUTE_SQL: contract_id or hypothesis_id or ActionType.EXECUTE_SQL.value,
        }[trace.tool_name]
        safe_error = trace.safe_error
        if safe_error is not None and safe_error not in {item.value for item in SafeSqlDiagnostic}:
            safe_error = "internal_error"
        refs.append(
            SafeToolTraceRef(
                tool_name=trace.tool_name,
                purpose=purpose,
                contract_id=contract_id,
                hypothesis_id=hypothesis_id,
                query_id=trace.query_id,
                columns=trace.columns,
                row_count=trace.row_count,
                possibly_truncated=trace.possibly_truncated,
                outcome="failed" if trace.safe_error is not None else "completed",
                safe_error=safe_error,
                profile_metadata=(
                    SafeProfileTraceMetadata(
                        table_name=cast(str | None, arguments.get("table_name")),
                        column_name=cast(str | None, arguments.get("column_name")),
                        operation=cast(Any, arguments.get("operation")),
                        filter_columns=cast(tuple[str, ...], arguments.get("filter_columns", ())),
                        has_time_window=cast(bool | None, arguments.get("has_time_window")),
                        limit=cast(int | None, arguments.get("limit")),
                        time_column=cast(str | None, arguments.get("time_column")),
                    )
                    if trace.tool_name is ActionType.PROFILE
                    else None
                ),
            )
        )
    return tuple(refs)


def _evidence_refs(
    result: AgentRunResult, required_purposes: tuple[str, ...]
) -> tuple[SafeEvidenceRef, ...]:
    if len({item.evidence_id for item in result.evidence}) != len(
        result.evidence
    ) or not execute_linkage_is_bijective(
        result.observations,
        result.observation_validations,
        result.safe_trace.tool_calls,
    ):
        return ()
    observations = {item.observation_id: item for item in result.observations}
    refs = []
    represented: set[str] = set()
    validations = {item.observation_id: item for item in result.observation_validations}
    for evidence in result.evidence:
        observation = observations.get(evidence.observation_id)
        validation = validations.get(evidence.observation_id)
        purpose = (
            None
            if observation is None
            else (
                observation.hypothesis_id
                if observation.hypothesis_id in required_purposes
                else observation.purpose
            )
        )
        if (
            evidence.verified
            and observation is not None
            and validation is not None
            and validation.valid
            and observation.contract_id == evidence.contract_id == validation.contract_id
            and observation.hypothesis_id == evidence.hypothesis_id
            and observation.query_id == evidence.query_id
            and restore_query_result(observation) is not None
            and purpose in required_purposes
            and purpose not in represented
        ):
            represented.add(purpose)
            refs.append(
                SafeEvidenceRef(
                    evidence_id=evidence.evidence_id,
                    observation_id=evidence.observation_id,
                    purpose=purpose,
                    contract_id=evidence.contract_id,
                    hypothesis_id=evidence.hypothesis_id,
                    query_id=evidence.query_id,
                )
            )
    return tuple(refs)


def _expected_execute_triples(case: Week3EvaluationCase) -> tuple[tuple[str, str, str], ...]:
    special = {
        "W3K027": (("metric_value_contract", "metric_value_contract", "metric_value"),) * 2,
        "W3K028": (("metric_value_contract", "metric_value_contract", "metric_value"),) * 2,
        "W3K029": (("gmv_comparison", "gmv_comparison", "confirm_decline"),),
        "W3K030": (("metric_value_contract", "metric_value_contract", "metric_value"),),
    }
    if case.case_id in special:
        return special[case.case_id]
    triples = []
    for expected in case.expected_observations:
        purpose = expected.purpose
        contract = "gmv_comparison" if purpose == "confirm_decline" else purpose
        hypothesis = "metric_value" if purpose == "metric_value_contract" else purpose
        triples.append((contract, contract, hypothesis))
    return tuple(triples)


def _suite_score(case: Week3EvaluationCase, result: AgentRunResult) -> SuiteScore:
    if case.case_id not in {"W3K027", "W3K028", "W3K029", "W3K030"}:
        return SuiteScore(suite=case.suite, conformant=True)
    links_ok = execute_linkage_is_bijective(
        result.observations, result.observation_validations, result.safe_trace.tool_calls
    )
    validations = {item.observation_id: item for item in result.observation_validations}
    matching = tuple(
        item
        for item in result.observations
        if item.tool_name is ActionType.EXECUTE_SQL
        and item.purpose in {"metric_value_contract", "gmv_comparison"}
    )
    if case.case_id in {"W3K027", "W3K028"}:
        first = matching[0] if matching else None
        first_validation = None if first is None else validations.get(first.observation_id)
        first_is_earliest = links_ok and result.first_candidate == first and len(matching) == 2
        first_invalid = first_validation is not None and not first_validation.valid
        valid = tuple(
            item
            for item in matching
            if (validation := validations.get(item.observation_id)) and validation.valid
        )
        final_met = len(valid) == 1 if case.case_id == "W3K027" else not valid
        return SuiteScore(
            suite=case.suite,
            first_is_earliest=first_is_earliest,
            first_validation_invalid=first_invalid,
            final_requirement_met=final_met,
            conformant=first_is_earliest and first_invalid and final_met,
        )
    if case.case_id == "W3K029":
        evidence = score_evidence(
            required_purposes=("confirm_decline",),
            evidence=result.evidence,
            observations=result.observations,
            validations=result.observation_validations,
            tool_calls=result.safe_trace.tool_calls,
        )
        conformant = links_ok and evidence.oracle_verified_sufficient
        return SuiteScore(
            suite=case.suite,
            verified_partial_evidence=conformant,
            conformant=conformant,
        )
    failed = tuple(
        item
        for item in result.safe_trace.tool_calls
        if item.tool_name is ActionType.EXECUTE_SQL and item.safe_error is not None
    )
    conformant = (
        len(failed) == 1
        and failed[0].safe_error in {item.value for item in SafeSqlDiagnostic}
        and not any(
            item.tool_name is ActionType.EXECUTE_SQL and item.ok for item in result.observations
        )
        and not result.evidence
        and result.governance.repair_count == 0
    )
    return SuiteScore(
        suite=case.suite,
        policy_rejection_conformant=conformant,
        conformant=conformant,
    )


def _score_case(
    case: Week3EvaluationCase,
    result: AgentRunResult,
    settings: AgentRuntimeSettings,
    *,
    expected_resolved_model: str,
    error_override: str | None = None,
) -> Week3CaseResult:
    behavior = score_behavior(
        expected_action=case.expected_behavior,
        expected_missing_fields=case.expected_missing_fields,
        behavior_action=None if result.behavior is None else result.behavior.action,
        behavior_reason=None if result.behavior is None else result.behavior.reason_code.value,
        observed_missing_fields=() if result.behavior is None else result.behavior.missing_fields,
        expected_stop_reason=None
        if case.expected_stop_reason is None
        else case.expected_stop_reason.value,
    )
    purposes = tuple(item.purpose for item in case.expected_observations)
    if case.case_id == "W3K029":
        purposes = ("confirm_decline",)
    tool = score_tool_trace(
        required_tools=case.required_tools,
        forbidden_tools=case.forbidden_tools,
        tool_calls=result.safe_trace.tool_calls,
        required_execute_triples=_expected_execute_triples(case),
    )
    evidence = score_evidence(
        required_purposes=purposes,
        evidence=result.evidence,
        observations=result.observations,
        validations=result.observation_validations,
        tool_calls=result.safe_trace.tool_calls,
    )
    first_scores: list[CandidateScore] = []
    final_scores: list[CandidateScore] = []
    for expected_index, expected_observation in enumerate(case.expected_observations):
        first, final, first_link, final_link = candidate_observations(
            result, purpose=expected_observation.purpose
        )
        expected = _read_expected(expected_observation.expected_result_path)
        candidates = [(final, final_link, final_scores)]
        if expected_index == 0:
            candidates.insert(0, (first, first_link, first_scores))
        for target, link, destination in candidates:
            restored = None if target is None else restore_query_result(target)
            actual = (
                None
                if restored is None
                else EvalQueryResult(columns=restored.columns, rows=restored.rows)
            )
            destination.append(
                score_candidate(
                    comparison=expected_observation.comparison,
                    key_columns=expected_observation.key_columns,
                    numeric_columns=expected_observation.numeric_columns,
                    expected=expected,
                    actual=actual,
                    answer_contract_ok=link,
                    execution_succeeded=restored is not None,
                    possibly_truncated=False if target is None else target.possibly_truncated,
                )
            )
    first_score, final_score = _combine(tuple(first_scores)), _combine(tuple(final_scores))
    limits = BudgetLimits.from_settings(settings)
    if case.budget_overrides is not None:
        if case.budget_overrides.max_tool_calls is not None:
            limits = replace(limits, max_tool_calls=case.budget_overrides.max_tool_calls)
        if case.budget_overrides.max_execute_calls is not None:
            limits = replace(limits, max_execute_calls=case.budget_overrides.max_execute_calls)
    governance = result.governance
    budget_ok = (
        governance.action_loops <= limits.max_action_loops
        and governance.llm_calls <= limits.max_llm_calls
        and governance.tool_calls <= limits.max_tool_calls
        and governance.execute_calls <= limits.max_execute_calls
        and governance.profile_calls <= limits.max_profile_calls
        and governance.repair_count <= limits.max_repairs
        and governance.repair_count == case.expected_repair_count
    )
    terminal_ok = (
        result.final_answer.status is case.expected_final_status
        and result.final_answer.stop_reason is case.expected_stop_reason
    )
    case_models = tuple(
        dict.fromkeys(item.provider_model for item in result.safe_trace.model_calls)
    )
    model_identity_complete = (
        bool(result.safe_trace.model_calls)
        and case_models == (expected_resolved_model,)
        and sum(item.input_tokens for item in result.safe_trace.model_calls)
        == governance.input_tokens
        and sum(item.output_tokens for item in result.safe_trace.model_calls)
        == governance.output_tokens
    )
    evidence_score = evidence if purposes else None
    suite_score = _suite_score(case, result)
    budget_score = BudgetScore(
        conformant=budget_ok,
        action_loops=governance.action_loops,
        llm_calls=governance.llm_calls,
        tool_calls=governance.tool_calls,
        execute_calls=governance.execute_calls,
        profile_calls=governance.profile_calls,
        repair_count=governance.repair_count,
        input_tokens=governance.input_tokens,
        output_tokens=governance.output_tokens,
        committed_cost_cny=governance.committed_cost_cny,
        soft_cap_reached=governance.soft_cap_reached,
        max_action_loops=limits.max_action_loops,
        max_llm_calls=limits.max_llm_calls,
        max_tool_calls=limits.max_tool_calls,
        max_execute_calls=limits.max_execute_calls,
        max_profile_calls=limits.max_profile_calls,
        max_repairs=limits.max_repairs,
        expected_repair_count=case.expected_repair_count,
        hard_cost_cny=limits.hard_cost_cny,
    )
    return Week3CaseResult(
        case_id=case.case_id,
        cohort=case.cohort,
        suite=case.suite,
        expected_behavior=BehaviorAction(case.expected_behavior),
        expected_missing_fields=case.expected_missing_fields,
        expected_final_status=case.expected_final_status,
        expected_stop_reason=cast(StopReason, case.expected_stop_reason),
        suite_score=suite_score,
        observed_behavior=None if result.behavior is None else result.behavior.action,
        observed_behavior_reason=(None if result.behavior is None else result.behavior.reason_code),
        observed_missing_fields=(() if result.behavior is None else result.behavior.missing_fields),
        observed_final_status=result.final_answer.status,
        observed_stop_reason=result.final_answer.stop_reason,
        error_type=error_override,
        first_candidate_score=first_score,
        final_candidate_score=final_score,
        behavior_score=behavior,
        tool_score=tool,
        evidence_score=evidence_score,
        budget_score=budget_score,
        safe_tool_trace=_safe_tool_refs(result),
        evidence_references=_evidence_refs(result, purposes),
        resolved_models=case_models,
        model_identity_complete=model_identity_complete,
        repair_succeeded=(
            case.case_id == "W3K027" and suite_score.conformant if case.suite == "repair" else None
        ),
        valid_execute_count=sum(item.valid for item in result.observation_validations),
        natural_refusal=(
            behavior.conformant and terminal_ok and not result.safe_trace.tool_calls
            if case.expected_behavior != "execute"
            else None
        ),
    )


def _pricing_hash(pricing: ModelPricing) -> str:
    encoded = json.dumps(
        pricing.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


async def run_week3_evaluation(
    *,
    mode: Literal["fixture", "live"],
    executor: Week3CaseExecutor,
    output_root: str | Path = "artifacts/evals/week3",
    cases: tuple[Week3EvaluationCase, ...] | None = None,
    pricing: ModelPricing | None = None,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
) -> Week3RunArtifact:
    if mode == "fixture" and pricing is not None:
        raise Week3RunError("fixture mode does not accept pricing")
    if mode == "live" and pricing is None:
        raise Week3RunError("live mode requires pricing")
    source_cases = load_week3_cases() if cases is None else cases
    active = tuple(case for case in source_cases if mode in case.modes)
    if not active:
        raise Week3RunError("no cases are enabled for this mode")
    generated = (now or (lambda: datetime.now(UTC)))()
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise Week3RunError("evaluation clock must be timezone-aware")
    generated = generated.astimezone(UTC)
    effective_run_id = run_id or f"week3-{generated.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    reservation = reserve_week3_report(
        output_root, mode=mode, run_id=effective_run_id, timestamp=generated
    )
    settings = getattr(executor, "settings", AgentRuntimeSettings.model_validate({}))
    try:
        outcomes: list[tuple[AgentRunResult, str | None]] = []
        for case in active:
            error_type: str | None
            try:
                outcome = await executor.run_case(case_id=case.case_id, question=case.question)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                outcome = _safe_failure(case_id=case.case_id, reason=StopReason.INTERNAL_ERROR)
                error_type = "internal_case_failure"
            else:
                if outcome.run_id != case.case_id:
                    outcome = _safe_failure(case_id=case.case_id, reason=StopReason.INTERNAL_ERROR)
                    error_type = "case_identity_mismatch"
                else:
                    error_type = None
            outcomes.append((outcome, error_type))
        effective_pricing = _fixture_pricing() if mode == "fixture" else cast(ModelPricing, pricing)
        scored_items: list[Week3CaseResult] = []
        for case, (outcome, error_type) in zip(active, outcomes, strict=True):
            try:
                scored_case = _score_case(
                    case,
                    outcome,
                    settings,
                    expected_resolved_model=effective_pricing.resolved_model,
                    error_override=error_type,
                )
            except Exception:
                scored_case = _score_case(
                    case,
                    _safe_failure(case_id=case.case_id, reason=StopReason.INTERNAL_ERROR),
                    settings,
                    expected_resolved_model=effective_pricing.resolved_model,
                    error_override="scoring_contract_failure",
                )
            scored_items.append(scored_case)
        scored = tuple(scored_items)
        resolved = tuple(
            dict.fromkeys(
                trace.provider_model
                for outcome, _error_type in outcomes
                for trace in outcome.safe_trace.model_calls
            )
        )
        requested = effective_pricing.requested_model
        limits = BudgetLimits.from_settings(settings)
        overall_manifest = week3_manifest_sha256()
        case_ids = tuple(case.case_id for case in active)
        canonical_ids = tuple(case.case_id for case in load_week3_cases() if mode in case.modes)
        report = Week3RunReport(
            mode=mode,
            overall_manifest_sha256=overall_manifest,
            known_cohort_sha256=week3_cohort_sha256("known"),
            heldout_cohort_sha256=week3_cohort_sha256("heldout"),
            executed_manifest_sha256=derive_executed_manifest_sha256(
                overall_manifest_sha256=overall_manifest,
                mode=mode,
                case_ids=case_ids,
            ),
            report_scope="canonical" if case_ids == canonical_ids else "partial_test",
            run_id=effective_run_id,
            generated_at_utc=generated,
            requested_model=requested,
            resolved_models=resolved,
            pricing_effective_date=effective_pricing.effective_date,
            pricing_snapshot_sha256=_pricing_hash(effective_pricing),
            budget_configuration=BudgetConfiguration(
                max_action_loops=limits.max_action_loops,
                max_llm_calls=limits.max_llm_calls,
                max_tool_calls=limits.max_tool_calls,
                max_execute_calls=limits.max_execute_calls,
                max_profile_calls=limits.max_profile_calls,
                max_repairs=limits.max_repairs,
                max_concurrent_runs=settings.max_concurrent_runs,
                timeout_seconds=limits.timeout_seconds,
                soft_cost_cny=limits.soft_cost_cny,
                hard_cost_cny=limits.hard_cost_cny,
            ),
            cases=scored,
        )
        published = write_week3_report(reservation, report)
        return Week3RunArtifact(
            report=report,
            report_dir=published.report_dir,
            report_json=published.report_json,
            report_markdown=published.report_markdown,
        )
    except BaseException:
        cancel_week3_report_reservation(reservation)
        raise


__all__ = [
    "FixtureWeek3CaseExecutor",
    "LiveWeek3CaseExecutor",
    "Week3CaseExecutor",
    "Week3RunArtifact",
    "Week3RunError",
    "run_week3_evaluation",
]
