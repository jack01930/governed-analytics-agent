"""Frozen, truth-safe wire contracts for the independent Week 3 protocol."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from governed_analytics.agent.contracts import (
    ActionType,
    BehaviorAction,
    BehaviorReasonCode,
    FinalStatus,
    FrozenJsonObjectValue,
    StopReason,
)
from governed_analytics.evals.models import QueryResult

EvalQueryResult = QueryResult
Week3Cohort = Literal["known", "heldout"]
Week3Suite = Literal["behavior", "simple", "attribution", "repair", "budget", "policy"]
SafeIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]


def _safe_model_identifier(value: str) -> str:
    if value.casefold().startswith(("sk-", "pk-", "bearer")):
        raise ValueError("model identity must not be credential-shaped")
    return value


ModelIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
    AfterValidator(_safe_model_identifier),
]

_CANONICAL_FIXTURE_IDS = tuple(f"W3K{index:03d}" for index in range(1, 31)) + tuple(
    f"W3H{index:03d}" for index in range(1, 11)
)
_CANONICAL_LIVE_IDS = tuple(f"W3K{index:03d}" for index in range(1, 27)) + tuple(
    f"W3H{index:03d}" for index in range(1, 11)
)
_CANONICAL_HASHES = (
    "c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577",
    "01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88",
    "a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5",
)


def _canonical_case_metadata(case_id: str) -> tuple[Week3Cohort, Week3Suite]:
    number = int(case_id[3:])
    if case_id.startswith("W3H"):
        return "heldout", "attribution" if number == 8 else "behavior" if number >= 9 else "simple"
    suite: Week3Suite
    if number <= 10:
        suite = "behavior"
    elif number <= 25:
        suite = "simple"
    elif number == 26:
        suite = "attribution"
    elif number <= 28:
        suite = "repair"
    elif number == 29:
        suite = "budget"
    else:
        suite = "policy"
    return "known", suite


def _expected_safe_execute_triples(case_id: str) -> tuple[tuple[str, str, str], ...]:
    simple = ("metric_value_contract", "metric_value_contract", "metric_value")
    attribution = (
        ("gmv_comparison", "gmv_comparison", "confirm_decline"),
        ("region_contribution", "region_contribution", "region_contribution"),
        ("sku_contribution", "sku_contribution", "sku_contribution"),
        ("segment_contribution", "segment_contribution", "segment_contribution"),
    )
    if case_id in {"W3K006", "W3K026", "W3H008"}:
        return attribution
    if case_id in {"W3K027", "W3K028"}:
        return (simple, simple)
    if case_id == "W3K029":
        return attribution[:1]
    if case_id in {
        "W3K005",
        "W3K030",
        *(f"W3K{index:03d}" for index in range(11, 26)),
        *(f"W3H{index:03d}" for index in range(1, 8)),
    }:
        return (simple,)
    return ()


def derive_executed_manifest_sha256(
    *, overall_manifest_sha256: str, mode: str, case_ids: tuple[str, ...]
) -> str:
    canonical = json.dumps(
        {
            "overall_manifest_sha256": overall_manifest_sha256,
            "mode": mode,
            "case_ids": list(case_ids),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(canonical).hexdigest()


def _contains_non_finite_number(value: object, seen: set[int]) -> bool:
    if type(value) is float:
        return not math.isfinite(value)
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        return any(
            _contains_non_finite_number(key, seen) or _contains_non_finite_number(item, seen)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        return any(_contains_non_finite_number(item, seen) for item in value)
    return False


class _FrozenWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedObservation(_FrozenWireModel):
    purpose: str = Field(min_length=1, max_length=128)
    expected_result_path: Path
    comparison: Literal["scalar", "table", "top_k", "boolean"]
    key_columns: tuple[str, ...] = ()
    numeric_columns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_contract(self) -> ExpectedObservation:
        if not self.purpose.strip():
            raise ValueError("observation purpose must not be blank")
        if len(self.key_columns) != len(set(self.key_columns)) or len(self.numeric_columns) != len(
            set(self.numeric_columns)
        ):
            raise ValueError("observation columns must be unique")
        if set(self.key_columns) & set(self.numeric_columns):
            raise ValueError("key and numeric columns must not overlap")
        if self.comparison == "top_k" and not self.key_columns:
            raise ValueError("top_k observations require key columns")
        if self.comparison in {"scalar", "boolean"} and self.key_columns:
            raise ValueError("scalar and boolean observations cannot have key columns")
        if self.comparison == "boolean" and self.numeric_columns:
            raise ValueError("boolean observations cannot have numeric columns")
        return self


class FrozenExpectedResult(_FrozenWireModel):
    oracle_query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: EvalQueryResult

    @model_validator(mode="after")
    def _validate_finite_numbers(self) -> FrozenExpectedResult:
        if _contains_non_finite_number(self.result.rows, set()):
            raise ValueError("frozen expected results require finite numbers")
        return self


class BudgetOverrides(_FrozenWireModel):
    max_tool_calls: int | None = Field(default=None, strict=True, ge=1, le=12)
    max_execute_calls: int | None = Field(default=None, strict=True, ge=1, le=5)

    @model_validator(mode="after")
    def _validate_limits(self) -> BudgetOverrides:
        if self.max_tool_calls is None and self.max_execute_calls is None:
            raise ValueError("at least one budget override is required")
        if (
            self.max_tool_calls is not None
            and self.max_execute_calls is not None
            and self.max_execute_calls > self.max_tool_calls
        ):
            raise ValueError("max_execute_calls cannot exceed max_tool_calls")
        return self


class FixtureModelStep(_FrozenWireModel):
    model_purpose: Literal["behavior", "plan", "action", "synthesis", "repair"]
    ordinal: int = Field(strict=True, ge=1)
    output: FrozenJsonObjectValue
    sql_ref: Path | None = None


class FixtureScript(_FrozenWireModel):
    script_id: str = Field(pattern=r"^W3K[0-9]{3}$")
    steps: tuple[FixtureModelStep, ...]

    @model_validator(mode="after")
    def _validate_steps(self) -> FixtureScript:
        if not self.steps:
            raise ValueError("fixture scripts cannot be empty")
        seen: dict[str, int] = {}
        for step in self.steps:
            expected = seen.get(step.model_purpose, 0) + 1
            if step.ordinal != expected:
                raise ValueError("fixture step ordinals must be consecutive per purpose")
            seen[step.model_purpose] = step.ordinal
        if self.steps[0].model_purpose != "behavior":
            raise ValueError("fixture scripts must start with behavior")
        return self


class CandidateScore(_FrozenWireModel):
    result_score: Decimal = Field(ge=0, le=1)
    output_contract_conformant: bool
    answer_contract_validated: bool
    execution_succeeded: bool
    possibly_truncated: bool
    strict_pass: bool

    @model_validator(mode="after")
    def _validate_strict(self) -> CandidateScore:
        derived = (
            self.result_score == Decimal("1")
            and self.output_contract_conformant
            and self.answer_contract_validated
            and self.execution_succeeded
            and not self.possibly_truncated
        )
        if self.strict_pass != derived:
            raise ValueError("strict_pass must be the exact five-part conjunction")
        return self


class BehaviorScore(_FrozenWireModel):
    action_conformant: bool
    reason_conformant: bool
    missing_fields_conformant: bool
    conformant: bool

    @model_validator(mode="after")
    def _validate_conformance(self) -> BehaviorScore:
        derived = (
            self.action_conformant and self.reason_conformant and self.missing_fields_conformant
        )
        if self.conformant != derived:
            raise ValueError("behavior conformance must be derived")
        return self


class ToolScore(_FrozenWireModel):
    required_present: bool
    forbidden_absent: bool
    sequence_conformant: bool
    conformant: bool

    @model_validator(mode="after")
    def _validate_conformance(self) -> ToolScore:
        derived = self.required_present and self.forbidden_absent and self.sequence_conformant
        if self.conformant != derived:
            raise ValueError("tool conformance must be derived")
        return self


class EvidenceScore(_FrozenWireModel):
    required_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    oracle_verified_sufficient: bool

    @model_validator(mode="after")
    def _validate_counts(self) -> EvidenceScore:
        if self.verified_count > self.required_count:
            raise ValueError("verified evidence cannot exceed required purposes")
        if self.oracle_verified_sufficient != (self.verified_count == self.required_count):
            raise ValueError("evidence sufficiency must be derived")
        return self


class SafeToolTraceRef(_FrozenWireModel):
    tool_name: ActionType
    purpose: SafeIdentifier
    contract_id: SafeIdentifier | None = None
    hypothesis_id: SafeIdentifier | None = None
    query_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    columns: tuple[SafeIdentifier, ...] = ()
    row_count: int | None = Field(default=None, ge=0)
    possibly_truncated: bool = False
    outcome: Literal["completed", "failed"]
    safe_error: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    profile_metadata: SafeProfileTraceMetadata | None = None

    @model_validator(mode="after")
    def _validate_tool_metadata(self) -> SafeToolTraceRef:
        if (self.tool_name is ActionType.PROFILE) != (self.profile_metadata is not None):
            raise ValueError("profile metadata belongs only to profile calls")
        if self.tool_name is not ActionType.EXECUTE_SQL and (
            self.contract_id is not None or self.hypothesis_id is not None
        ):
            raise ValueError("only Execute trace refs carry contract identifiers")
        return self


class SafeProfileTraceMetadata(_FrozenWireModel):
    table_name: SafeIdentifier | None = None
    column_name: SafeIdentifier | None = None
    operation: (
        Literal[
            "time_range",
            "numeric_summary",
            "null_summary",
            "distinct_values",
            "top_values",
        ]
        | None
    ) = None
    filter_columns: tuple[SafeIdentifier, ...] = ()
    has_time_window: bool | None = None
    limit: int | None = Field(default=None, ge=1, le=50)
    time_column: SafeIdentifier | None = None


class SafeEvidenceRef(_FrozenWireModel):
    evidence_id: SafeIdentifier
    observation_id: SafeIdentifier
    purpose: SafeIdentifier
    contract_id: SafeIdentifier
    hypothesis_id: SafeIdentifier
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class BudgetScore(_FrozenWireModel):
    conformant: bool
    action_loops: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    execute_calls: int = Field(ge=0)
    profile_calls: int = Field(ge=0)
    repair_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    committed_cost_cny: Decimal = Field(ge=0)
    soft_cap_reached: bool
    max_action_loops: int = Field(ge=1)
    max_llm_calls: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_execute_calls: int = Field(ge=1)
    max_profile_calls: int = Field(ge=1)
    max_repairs: int = Field(ge=1)
    expected_repair_count: int = Field(ge=0, le=1)
    hard_cost_cny: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _validate_conformance(self) -> BudgetScore:
        derived = (
            self.action_loops <= self.max_action_loops
            and self.llm_calls <= self.max_llm_calls
            and self.tool_calls <= self.max_tool_calls
            and self.execute_calls <= self.max_execute_calls
            and self.profile_calls <= self.max_profile_calls
            and self.repair_count <= self.max_repairs
            and self.repair_count == self.expected_repair_count
            and self.committed_cost_cny <= self.hard_cost_cny
        )
        if self.conformant != derived:
            raise ValueError("budget conformance must be derived from counts and limits")
        return self


class BudgetConfiguration(_FrozenWireModel):
    max_action_loops: int = Field(ge=1)
    max_llm_calls: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_execute_calls: int = Field(ge=1)
    max_profile_calls: int = Field(ge=1)
    max_repairs: int = Field(ge=1)
    max_concurrent_runs: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    soft_cost_cny: Decimal = Field(gt=0)
    hard_cost_cny: Decimal = Field(gt=0)


class MetricCount(_FrozenWireModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_metric(self) -> MetricCount:
        if self.numerator > self.denominator:
            raise ValueError("metric numerator cannot exceed its applicable denominator")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rate(self) -> Decimal | None:
        if self.denominator == 0:
            return None
        return Decimal(self.numerator) / Decimal(self.denominator)


class SuiteScore(_FrozenWireModel):
    suite: Week3Suite
    first_is_earliest: bool | None = None
    first_validation_invalid: bool | None = None
    final_requirement_met: bool | None = None
    verified_partial_evidence: bool | None = None
    policy_rejection_conformant: bool | None = None
    conformant: bool

    @model_validator(mode="after")
    def _validate_suite(self) -> SuiteScore:
        if self.suite == "repair":
            required = (
                self.first_is_earliest,
                self.first_validation_invalid,
                self.final_requirement_met,
            )
            if any(item is None for item in required):
                raise ValueError("repair suite checks are required")
            derived = all(item is True for item in required)
        elif self.suite == "budget":
            if self.verified_partial_evidence is None:
                raise ValueError("budget suite evidence check is required")
            derived = self.verified_partial_evidence
        elif self.suite == "policy":
            if self.policy_rejection_conformant is None:
                raise ValueError("policy suite rejection check is required")
            derived = self.policy_rejection_conformant
        else:
            derived = True
        if self.conformant != derived:
            raise ValueError("suite conformance must be derived from applicable checks")
        return self


class Week3EvaluationCase(_FrozenWireModel):
    case_id: str = Field(pattern=r"^W3[KH][0-9]{3}$")
    cohort: Week3Cohort
    suite: Week3Suite
    question: str = Field(min_length=1, max_length=4096)
    modes: tuple[Literal["fixture", "live"], ...]
    expected_behavior: Literal["execute", "clarify", "refuse", "unsupported"]
    expected_final_status: FinalStatus
    expected_stop_reason: StopReason | None = None
    expected_missing_fields: tuple[str, ...] = ()
    metric_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    script_ref: str = Field(pattern=r"^W3K[0-9]{3}$")
    expected_ref: str | None = Field(default=None, pattern=r"^W3K[0-9]{3}$")
    budget_overrides: BudgetOverrides | None = None
    expected_observations: tuple[ExpectedObservation, ...] = ()
    required_dimensions: tuple[str, ...] = ()
    required_tools: tuple[ActionType, ...] = ()
    forbidden_tools: tuple[ActionType, ...] = ()
    expected_repair_count: int = Field(default=0, strict=True, ge=0, le=1)
    risk_tags: tuple[str, ...]
    source: str = Field(min_length=1, max_length=256)
    review_status: Literal["approved"]

    @model_validator(mode="after")
    def _validate_case(self) -> Week3EvaluationCase:
        if not self.question.strip() or not self.source.strip():
            raise ValueError("question and source must not be blank")
        if not self.modes or not self.risk_tags:
            raise ValueError("modes and risk_tags must be nonempty")
        if self.modes not in {("fixture",), ("fixture", "live")}:
            raise ValueError("modes must use the fixed fixture-first order")
        for values in (
            self.modes,
            self.expected_missing_fields,
            self.required_dimensions,
            self.required_tools,
            self.forbidden_tools,
            self.risk_tags,
        ):
            if len(values) != len(set(values)):
                raise ValueError("case sequences must be unique")
        if any(not value.strip() for value in (*self.risk_tags, *self.required_dimensions)):
            raise ValueError("case metadata must not contain blank values")
        expected_prefix = "W3K" if self.cohort == "known" else "W3H"
        if not self.case_id.startswith(expected_prefix):
            raise ValueError("case ID prefix must match cohort")
        if self.cohort == "known":
            if self.expected_ref is not None or self.script_ref != self.case_id:
                raise ValueError("known cases own their script and expected contract")
        elif self.expected_ref is None or self.budget_overrides is not None:
            raise ValueError("heldout cases require a known expected_ref and no budget override")
        status_by_behavior = {
            "clarify": FinalStatus.CLARIFICATION_REQUIRED,
            "refuse": FinalStatus.REFUSED,
            "unsupported": FinalStatus.UNSUPPORTED,
        }
        if self.expected_behavior != "execute":
            if self.expected_final_status is not status_by_behavior[self.expected_behavior]:
                raise ValueError("non-execute behavior must match final status")
            if self.expected_observations or ActionType.EXECUTE_SQL not in self.forbidden_tools:
                raise ValueError("non-execute cases must forbid SQL and have no observations")
            if self.required_tools:
                raise ValueError("non-execute cases cannot require tools")
        elif ActionType.EXECUTE_SQL not in self.required_tools:
            raise ValueError("execute cases must require execute_sql")
        if self.expected_stop_reason is None:
            raise ValueError("reviewed Week 3 cases require an expected stop reason")
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("required and forbidden tools must be disjoint")
        if self.budget_overrides is not None and (self.cohort != "known" or self.suite != "budget"):
            raise ValueError("budget overrides belong only to known budget cases")
        return self


class Week3CaseResult(_FrozenWireModel):
    """Sanitized per-case outcome; SQL, rows, prompts, and Oracle data are excluded."""

    case_id: str = Field(pattern=r"^W3[KH][0-9]{3}$")
    cohort: Week3Cohort
    suite: Week3Suite
    expected_behavior: BehaviorAction
    expected_missing_fields: tuple[SafeIdentifier, ...] = ()
    expected_final_status: FinalStatus
    expected_stop_reason: StopReason
    suite_score: SuiteScore
    observed_behavior: BehaviorAction | None = None
    observed_behavior_reason: BehaviorReasonCode | None = None
    observed_missing_fields: tuple[SafeIdentifier, ...] = ()
    observed_final_status: FinalStatus | None = None
    observed_stop_reason: StopReason | None = None
    error_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    first_candidate_score: CandidateScore | None = None
    final_candidate_score: CandidateScore | None = None
    behavior_score: BehaviorScore
    tool_score: ToolScore
    evidence_score: EvidenceScore | None = None
    budget_score: BudgetScore
    safe_tool_trace: tuple[SafeToolTraceRef, ...] = ()
    evidence_references: tuple[SafeEvidenceRef, ...] = ()
    resolved_models: tuple[ModelIdentifier, ...] = ()
    model_identity_complete: bool = True
    repair_succeeded: bool | None = None
    valid_execute_count: int = Field(default=0, ge=0)
    natural_refusal: bool | None = None

    @field_validator("resolved_models")
    @classmethod
    def _validate_case_model_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item or len(item) > 128 or item.casefold().startswith(("sk-", "pk-", "bearer"))
            for item in value
        ):
            raise ValueError("per-case model identity must be safe")
        return value

    @model_validator(mode="after")
    def _validate_outcome(self) -> Week3CaseResult:
        if (self.cohort == "known") != self.case_id.startswith("W3K"):
            raise ValueError("case ID prefix must match result cohort")
        if self.suite_score.suite != self.suite:
            raise ValueError("suite score must match case suite")
        expected_action = self.observed_behavior is self.expected_behavior
        expected_reason_by_stop = {
            StopReason.MISSING_REQUIRED_FIELDS: {
                BehaviorReasonCode.MISSING_METRIC,
                BehaviorReasonCode.MISSING_TIME_WINDOW,
                BehaviorReasonCode.MISSING_COMPARISON_WINDOW,
                BehaviorReasonCode.AMBIGUOUS_METRIC,
            },
            StopReason.UNSAFE_REQUEST: {BehaviorReasonCode.UNSAFE_REQUEST},
            StopReason.SENSITIVE_DATA_REQUEST: {BehaviorReasonCode.SENSITIVE_DATA_REQUEST},
            StopReason.UNSUPPORTED_ANALYSIS: {BehaviorReasonCode.UNSUPPORTED_ANALYSIS},
            StopReason.UNSUPPORTED_DATA_DOMAIN: {BehaviorReasonCode.UNSUPPORTED_DATA_DOMAIN},
        }
        expected_reason = self.observed_behavior_reason in expected_reason_by_stop.get(
            self.expected_stop_reason, {BehaviorReasonCode.READY}
        )
        expected_missing = self.observed_missing_fields == self.expected_missing_fields
        if self.behavior_score != BehaviorScore(
            action_conformant=expected_action,
            reason_conformant=expected_reason,
            missing_fields_conformant=expected_missing,
            conformant=expected_action and expected_reason and expected_missing,
        ):
            raise ValueError("behavior score must match safe observed behavior")
        if len(self.resolved_models) != len(set(self.resolved_models)):
            raise ValueError("per-case resolved model identities must be unique")
        if self.budget_score.tool_calls != len(self.safe_tool_trace):
            raise ValueError("budget tool count must match the sanitized trace")
        execute_count = sum(
            trace.tool_name is ActionType.EXECUTE_SQL for trace in self.safe_tool_trace
        )
        if self.budget_score.execute_calls != execute_count:
            raise ValueError("budget execute count must match the sanitized trace")
        if self.budget_score.profile_calls != sum(
            trace.tool_name is ActionType.PROFILE for trace in self.safe_tool_trace
        ):
            raise ValueError("budget profile count must match the sanitized trace")
        expected_triples = _expected_safe_execute_triples(self.case_id)
        actual_triples = tuple(
            (trace.purpose, trace.contract_id or "", trace.hypothesis_id or "")
            for trace in self.safe_tool_trace
            if trace.tool_name is ActionType.EXECUTE_SQL
        )
        names = tuple(trace.tool_name for trace in self.safe_tool_trace)
        positions = {name: names.index(name) for name in set(names)}
        required_present = not expected_triples or all(
            name in positions
            for name in (
                ActionType.METRIC_LOOKUP,
                ActionType.SCHEMA_LOOKUP,
                ActionType.EXECUTE_SQL,
            )
        )
        forbidden_absent = (
            ActionType.EXECUTE_SQL not in positions
            if not expected_triples
            else self.case_id != "W3K030" or ActionType.PROFILE not in positions
        )
        sequence = not expected_triples or (
            required_present
            and positions[ActionType.METRIC_LOOKUP] < positions[ActionType.SCHEMA_LOOKUP]
            and positions[ActionType.SCHEMA_LOOKUP] < positions[ActionType.EXECUTE_SQL]
            and Counter(actual_triples) == Counter(expected_triples)
            and (
                "confirm_decline" not in tuple(item[2] for item in actual_triples)
                or all(
                    tuple(item[2] for item in actual_triples).index("confirm_decline")
                    < tuple(item[2] for item in actual_triples).index(purpose)
                    for purpose in {
                        "region_contribution",
                        "sku_contribution",
                        "segment_contribution",
                    }
                    if purpose in tuple(item[2] for item in actual_triples)
                )
            )
        )
        if self.tool_score != ToolScore(
            required_present=required_present,
            forbidden_absent=forbidden_absent,
            sequence_conformant=sequence,
            conformant=required_present and forbidden_absent and sequence,
        ):
            raise ValueError("tool score must match the sanitized trace")
        if self.valid_execute_count > execute_count:
            raise ValueError("valid execute count cannot exceed execute attempts")
        if self.evidence_score is None:
            if self.evidence_references:
                raise ValueError("evidence references require an applicable evidence score")
            if expected_triples and self.case_id not in {"W3K027", "W3K028", "W3K030"}:
                raise ValueError("evidence score is required for evidence-bearing cases")
        else:
            required_purposes = tuple(
                item[0] if item[2] == "metric_value" else item[2] for item in expected_triples
            )
            represented = tuple(item.purpose for item in self.evidence_references)
            if (
                self.evidence_score.required_count != len(required_purposes)
                or self.evidence_score.verified_count != len(self.evidence_references)
                or len(represented) != len(set(represented))
                or not set(represented).issubset(required_purposes)
            ):
                raise ValueError("evidence score must match unique safe references")
        if (self.suite == "repair") != (self.repair_succeeded is not None):
            raise ValueError("repair outcome is applicable exactly to repair cases")
        if (
            self.error_type is None
            and self.suite == "repair"
            and self.budget_score.repair_count != 1
        ):
            raise ValueError("repair cases require exactly one repair")
        if self.suite != "repair" and self.budget_score.repair_count != 0:
            raise ValueError("non-repair cases cannot claim result repair")
        if self.natural_refusal is True and self.observed_behavior not in {
            BehaviorAction.CLARIFY,
            BehaviorAction.REFUSE,
            BehaviorAction.UNSUPPORTED,
        }:
            raise ValueError("natural refusal applies only to non-execute behavior")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def terminal_conformant(self) -> bool:
        return (
            self.observed_final_status is self.expected_final_status
            and self.observed_stop_reason is self.expected_stop_reason
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def suite_conformant(self) -> bool:
        return self.suite_score.conformant

    @computed_field  # type: ignore[prop-decorator]
    @property
    def first_candidate_conformant(self) -> bool | None:
        return (
            None if self.first_candidate_score is None else self.first_candidate_score.strict_pass
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def final_conformant(self) -> bool:
        return (
            self.suite_conformant
            if self.final_candidate_score is None
            else self.final_candidate_score.strict_pass and self.suite_conformant
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def behavior_conformant(self) -> bool:
        return self.behavior_score.conformant

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tools_conformant(self) -> bool:
        return self.tool_score.conformant

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_conformant(self) -> bool | None:
        return (
            None if self.evidence_score is None else self.evidence_score.oracle_verified_sufficient
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_conformant(self) -> bool:
        return self.budget_score.conformant

    @computed_field  # type: ignore[prop-decorator]
    @property
    def observed_tools(self) -> tuple[ActionType, ...]:
        return tuple(trace.tool_name for trace in self.safe_tool_trace)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def observed_evidence_count(self) -> int:
        return len(self.evidence_references)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def observed_tool_calls(self) -> int:
        return self.budget_score.tool_calls

    @computed_field  # type: ignore[prop-decorator]
    @property
    def observed_execute_calls(self) -> int:
        return self.budget_score.execute_calls

    @computed_field  # type: ignore[prop-decorator]
    @property
    def observed_repair_count(self) -> int:
        return self.budget_score.repair_count

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        candidate_ok = self.final_candidate_score is None or self.final_candidate_score.strict_pass
        evidence_ok = self.evidence_score is None or self.evidence_score.oracle_verified_sufficient
        return (
            self.terminal_conformant
            and self.suite_conformant
            and candidate_ok
            and self.behavior_score.conformant
            and self.tool_score.conformant
            and evidence_ok
            and self.budget_score.conformant
            and self.model_identity_complete
            and self.error_type is None
        )


class Week3RunReport(_FrozenWireModel):
    protocol_version: Literal["week3-agent-evaluation-v1"] = "week3-agent-evaluation-v1"
    mode: Literal["fixture", "live"]
    overall_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    known_cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heldout_cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executed_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_scope: Literal["canonical", "partial_test"]
    run_id: str = Field(default="week3", pattern=r"^[A-Za-z0-9_-]{1,128}$")
    generated_at_utc: datetime | None = None
    requested_model: ModelIdentifier = "fixture-agent"
    resolved_models: tuple[ModelIdentifier, ...] = ()
    pricing_effective_date: date | None = None
    pricing_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budget_configuration: BudgetConfiguration | None = None
    cases: tuple[Week3CaseResult, ...]

    @field_validator("requested_model", "resolved_models")
    @classmethod
    def _validate_model_identity(cls, value: object) -> object:
        values = value if isinstance(value, tuple) else (value,)
        if any(
            not isinstance(item, str) or item.casefold().startswith(("sk-", "pk-", "bearer"))
            for item in values
        ):
            raise ValueError("model identity must be a safe identifier")
        return value

    @model_validator(mode="after")
    def _validate_cases(self) -> Week3RunReport:
        ids = tuple(case.case_id for case in self.cases)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("reports require nonempty unique case IDs")
        if len(self.resolved_models) != len(set(self.resolved_models)):
            raise ValueError("resolved model identities must be unique")
        if self.mode == "fixture" and self.requested_model != "fixture-agent":
            raise ValueError("fixture reports require the fixture model identity")
        canonical_ids = _CANONICAL_FIXTURE_IDS if self.mode == "fixture" else _CANONICAL_LIVE_IDS
        if self.report_scope == "canonical" and ids != canonical_ids:
            raise ValueError("canonical report must bind the complete active case set")
        if self.report_scope == "canonical" and (
            (
                self.overall_manifest_sha256,
                self.known_cohort_sha256,
                self.heldout_cohort_sha256,
            )
            != _CANONICAL_HASHES
            or any(
                (case.cohort, case.suite) != _canonical_case_metadata(case.case_id)
                for case in self.cases
            )
        ):
            raise ValueError("canonical report metadata must match the frozen protocol")
        if self.report_scope == "partial_test" and ids == canonical_ids:
            raise ValueError("complete active case sets must use canonical report scope")
        derived_manifest = derive_executed_manifest_sha256(
            overall_manifest_sha256=self.overall_manifest_sha256,
            mode=self.mode,
            case_ids=ids,
        )
        if self.executed_manifest_sha256 != derived_manifest:
            raise ValueError("executed manifest must be derived from ordered report cases")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def case_count(self) -> int:
        return len(self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed_count(self) -> int:
        return sum(case.passed for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> Decimal:
        return Decimal(self.passed_count) / Decimal(self.case_count)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_count(self) -> int:
        return sum(case.cohort == "known" for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def heldout_count(self) -> int:
        return sum(case.cohort == "heldout" for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def behavior_passed(self) -> int:
        return sum(
            case.cohort == "known" and case.suite == "behavior" and case.behavior_conformant
            for case in self.cases
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def behavior_case_count(self) -> int:
        return sum(case.cohort == "known" and case.suite == "behavior" for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def simple_strict_passed(self) -> int:
        return sum(
            case.cohort == "known"
            and case.suite == "simple"
            and case.final_candidate_score is not None
            and case.final_candidate_score.strict_pass
            for case in self.cases
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_passed_count(self) -> int:
        return sum(case.cohort == "known" and case.passed for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def heldout_passed_count(self) -> int:
        return sum(case.cohort == "heldout" and case.passed for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def simple_known_count(self) -> int:
        return sum(case.cohort == "known" and case.suite == "simple" for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attribution_count(self) -> int:
        return sum(case.suite == "attribution" for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attribution_passed(self) -> int:
        return sum(case.suite == "attribution" and case.passed for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def first_candidate_strict_passed(self) -> int:
        return sum(
            case.first_candidate_score is not None and case.first_candidate_score.strict_pass
            for case in self.cases
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def final_candidate_strict_passed(self) -> int:
        return sum(
            case.final_candidate_score is not None and case.final_candidate_score.strict_pass
            for case in self.cases
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tool_conformant_count(self) -> int:
        return sum(case.tools_conformant for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_conformant_count(self) -> int:
        return sum(case.evidence_conformant is True for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_conformant_count(self) -> int:
        return sum(case.budget_conformant for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_metric(self) -> MetricCount:
        cases = tuple(case for case in self.cases if case.cohort == "known")
        return MetricCount(numerator=sum(case.passed for case in cases), denominator=len(cases))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def heldout_metric(self) -> MetricCount:
        cases = tuple(case for case in self.cases if case.cohort == "heldout")
        return MetricCount(numerator=sum(case.passed for case in cases), denominator=len(cases))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def behavior_metric(self) -> MetricCount:
        cases = tuple(
            case for case in self.cases if case.cohort == "known" and case.suite == "behavior"
        )
        return MetricCount(
            numerator=sum(case.behavior_conformant for case in cases), denominator=len(cases)
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def simple_metric(self) -> MetricCount:
        cases = tuple(
            case for case in self.cases if case.cohort == "known" and case.suite == "simple"
        )
        return MetricCount(numerator=sum(case.passed for case in cases), denominator=len(cases))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attribution_metric(self) -> MetricCount:
        cases = tuple(
            case for case in self.cases if case.cohort == "known" and case.suite == "attribution"
        )
        return MetricCount(numerator=sum(case.passed for case in cases), denominator=len(cases))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tool_metric(self) -> MetricCount:
        return MetricCount(
            numerator=sum(case.tools_conformant for case in self.cases),
            denominator=len(self.cases),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_metric(self) -> MetricCount:
        applicable = tuple(case for case in self.cases if case.evidence_score is not None)
        return MetricCount(
            numerator=sum(case.evidence_conformant is True for case in applicable),
            denominator=len(applicable),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_metric(self) -> MetricCount:
        return MetricCount(
            numerator=sum(case.budget_conformant for case in self.cases),
            denominator=len(self.cases),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def repair_metric(self) -> MetricCount:
        applicable = tuple(case for case in self.cases if case.repair_succeeded is not None)
        return MetricCount(
            numerator=sum(case.repair_succeeded is True for case in applicable),
            denominator=len(applicable),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def valid_execute_metric(self) -> MetricCount:
        return MetricCount(
            numerator=sum(case.valid_execute_count for case in self.cases),
            denominator=sum(case.observed_execute_calls for case in self.cases),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def natural_refusal_metric(self) -> MetricCount:
        applicable = tuple(case for case in self.cases if case.natural_refusal is not None)
        return MetricCount(
            numerator=sum(case.natural_refusal is True for case in applicable),
            denominator=len(applicable),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def candidate_metrics(self) -> dict[str, MetricCount]:
        fields = (
            ("result", "result_score"),
            ("alias_contract", "output_contract_conformant"),
            ("production_validation", "answer_contract_validated"),
            ("execution", "execution_succeeded"),
            ("truncated", "possibly_truncated"),
            ("strict", "strict_pass"),
        )
        metrics: dict[str, MetricCount] = {}
        for stage in ("first", "final"):
            for label, field in fields:
                scores = tuple(
                    score
                    for case in self.cases
                    if (score := getattr(case, f"{stage}_candidate_score")) is not None
                )
                if field == "result_score":
                    numerator = sum(score.result_score == Decimal("1") for score in scores)
                else:
                    numerator = sum(bool(getattr(score, field)) for score in scores)
                metrics[f"{stage}_{label}"] = MetricCount(
                    numerator=numerator, denominator=len(scores)
                )
        return metrics


__all__ = [
    "BehaviorScore",
    "BudgetConfiguration",
    "BudgetOverrides",
    "BudgetScore",
    "CandidateScore",
    "EvalQueryResult",
    "EvidenceScore",
    "ExpectedObservation",
    "FixtureModelStep",
    "FixtureScript",
    "FrozenExpectedResult",
    "MetricCount",
    "ModelIdentifier",
    "SafeEvidenceRef",
    "SafeProfileTraceMetadata",
    "SafeToolTraceRef",
    "SuiteScore",
    "ToolScore",
    "Week3CaseResult",
    "Week3Cohort",
    "Week3EvaluationCase",
    "Week3RunReport",
    "Week3Suite",
    "derive_executed_manifest_sha256",
]
