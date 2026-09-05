"""Frozen, truth-safe wire contracts for the independent Week 3 protocol."""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from governed_analytics.agent.contracts import (
    ActionType,
    BehaviorAction,
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
            _contains_non_finite_number(key, seen)
            or _contains_non_finite_number(item, seen)
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
    observed_final_status: FinalStatus | None = None
    observed_stop_reason: StopReason | None = None
    observed_tools: tuple[ActionType, ...] = ()
    observed_evidence_count: int = Field(default=0, ge=0)
    observed_tool_calls: int = Field(default=0, ge=0)
    observed_execute_calls: int = Field(default=0, ge=0)
    observed_repair_count: int = Field(default=0, ge=0)
    error_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def _validate_outcome(self) -> Week3CaseResult:
        if (self.cohort == "known") != self.case_id.startswith("W3K"):
            raise ValueError("case ID prefix must match result cohort")
        if self.observed_execute_calls > self.observed_tool_calls:
            raise ValueError("execute calls cannot exceed tool calls")
        checks = (
            self.first_candidate_conformant,
            self.final_conformant,
            self.behavior_conformant,
            self.tools_conformant,
            self.evidence_conformant,
            self.budget_conformant,
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
    cases: tuple[Week3CaseResult, ...]

    @model_validator(mode="after")
    def _validate_cases(self) -> Week3RunReport:
        ids = tuple(case.case_id for case in self.cases)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("reports require nonempty unique case IDs")
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


__all__ = [
    "BudgetOverrides",
    "EvalQueryResult",
    "ExpectedObservation",
    "FixtureModelStep",
    "FixtureScript",
    "FrozenExpectedResult",
    "Week3CaseResult",
    "Week3Cohort",
    "Week3EvaluationCase",
    "Week3RunReport",
    "Week3Suite",
]
