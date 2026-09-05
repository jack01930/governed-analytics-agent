"""Frozen, truth-safe wire contracts for the independent Week 3 protocol."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

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
    purpose: str = Field(min_length=1, max_length=128)
    contract_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    hypothesis_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    query_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    columns: tuple[str, ...] = ()
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
    table_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    column_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
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
    filter_columns: tuple[str, ...] = ()
    has_time_window: bool | None = None
    limit: int | None = Field(default=None, ge=1, le=50)
    time_column: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class SafeEvidenceRef(_FrozenWireModel):
    evidence_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    observation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    purpose: str = Field(min_length=1, max_length=128)
    contract_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    hypothesis_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
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
    passed: bool
    first_candidate_conformant: bool | None = None
    final_conformant: bool
    behavior_conformant: bool
    tools_conformant: bool
    evidence_conformant: bool | None = None
    budget_conformant: bool
    observed_behavior: BehaviorAction | None = None
    observed_behavior_reason: BehaviorReasonCode | None = None
    observed_missing_fields: tuple[str, ...] = ()
    observed_final_status: FinalStatus | None = None
    observed_stop_reason: StopReason | None = None
    observed_tools: tuple[ActionType, ...] = ()
    observed_evidence_count: int = Field(default=0, ge=0)
    observed_tool_calls: int = Field(default=0, ge=0)
    observed_execute_calls: int = Field(default=0, ge=0)
    observed_repair_count: int = Field(default=0, ge=0)
    error_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    first_candidate_score: CandidateScore | None = None
    final_candidate_score: CandidateScore | None = None
    behavior_score: BehaviorScore | None = None
    tool_score: ToolScore | None = None
    evidence_score: EvidenceScore | None = None
    budget_score: BudgetScore | None = None
    safe_tool_trace: tuple[SafeToolTraceRef, ...] = ()
    evidence_references: tuple[SafeEvidenceRef, ...] = ()
    resolved_models: tuple[str, ...] = ()
    model_identity_complete: bool = True

    @field_validator("resolved_models")
    @classmethod
    def _validate_case_model_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item or len(item) > 128 or item.casefold().startswith(("sk-", "pk-", "bearer-"))
            for item in value
        ):
            raise ValueError("per-case model identity must be safe")
        return value

    @model_validator(mode="after")
    def _validate_outcome(self) -> Week3CaseResult:
        if (self.cohort == "known") != self.case_id.startswith("W3K"):
            raise ValueError("case ID prefix must match result cohort")
        if self.observed_execute_calls > self.observed_tool_calls:
            raise ValueError("execute calls cannot exceed tool calls")
        if len(self.resolved_models) != len(set(self.resolved_models)):
            raise ValueError("per-case resolved model identities must be unique")
        checks = (
            self.first_candidate_conformant,
            self.final_conformant,
            self.behavior_conformant,
            self.tools_conformant,
            self.evidence_conformant,
            self.budget_conformant,
            self.model_identity_complete,
        )
        derived_pass = all(item is not False for item in checks) and self.error_type is None
        if self.passed != derived_pass:
            raise ValueError("passed must be derived from the case conformance fields")
        return self


class Week3RunReport(_FrozenWireModel):
    protocol_version: Literal["week3-agent-evaluation-v1"] = "week3-agent-evaluation-v1"
    mode: Literal["fixture", "live"]
    overall_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    known_cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heldout_cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executed_manifest_sha256: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(default="week3", pattern=r"^[A-Za-z0-9_-]{1,128}$")
    generated_at_utc: datetime | None = None
    requested_model: str = Field(
        default="fixture-agent", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    resolved_models: tuple[str, ...] = ()
    pricing_effective_date: date | None = None
    pricing_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budget_configuration: BudgetConfiguration | None = None
    cases: tuple[Week3CaseResult, ...]

    @field_validator("requested_model", "resolved_models")
    @classmethod
    def _validate_model_identity(cls, value: object) -> object:
        values = value if isinstance(value, tuple) else (value,)
        if any(
            not isinstance(item, str) or item.casefold().startswith(("sk-", "pk-", "bearer-"))
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
        canonical = json.dumps(
            {"mode": self.mode, "case_ids": list(ids)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        derived_manifest = sha256(canonical).hexdigest()
        if self.executed_manifest_sha256 != "0" * 64 and (
            self.executed_manifest_sha256 != derived_manifest
        ):
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
    "SafeEvidenceRef",
    "SafeProfileTraceMetadata",
    "SafeToolTraceRef",
    "ToolScore",
    "Week3CaseResult",
    "Week3Cohort",
    "Week3EvaluationCase",
    "Week3RunReport",
    "Week3Suite",
]
