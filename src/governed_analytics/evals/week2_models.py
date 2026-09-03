"""Immutable Week 2 evaluation and report contracts, separate from core-v1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from governed_analytics.evals.models import NormalizedFinishReason

type SuiteName = Literal["core-v2", "paraphrase", "boundary", "safety"]
type Week2CaseStatus = Literal[
    "passed",
    "wrong_answer",
    "contract_violation",
    "wrong_answer_and_contract_violation",
    "invalid_sql",
    "execution_error",
    "rejected",
    "rejection_mismatch",
    "unexpected_accept",
]

WEEK2_FIXTURE_MODEL = "fixture-oracle"
WEEK2_PRICING_BASIS = "peak cache-miss upper bound converted at USD/CNY 6.7809"

_EXECUTE_SUITES = frozenset({"core-v2", "paraphrase", "boundary"})
_SCORE_STATUSES = frozenset(
    {
        "passed",
        "wrong_answer",
        "contract_violation",
        "wrong_answer_and_contract_violation",
    }
)
_ERROR_STATUSES = frozenset(
    {"invalid_sql", "execution_error", "rejection_mismatch", "unexpected_accept"}
)
_INVALID_SQL_ERRORS = frozenset(
    {
        "generation_invalid_request",
        "generation_provider_call_failed",
        "generation_missing_content",
        "generation_invalid_content",
        "generation_invalid_content_type",
        "generation_invalid_json",
        "generation_invalid_envelope",
        "generation_invalid_sql_content",
        "generation_invalid_assumptions",
        "generation_missing_usage",
        "generation_invalid_usage",
        "generation_missing_model",
        "generation_failed",
        "pricing_failed",
        "sql_rejected",
    }
)
_EXECUTION_ERRORS = frozenset({"query_timeout", "execution_failed", "scoring_failed"})
_SAFE_RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_SAFE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$"
_SAFE_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SECRET_PREFIXES = ("sk-", "pk-", "bearer-")


def _has_secret_prefix(value: str) -> bool:
    return value.lower().startswith(_SECRET_PREFIXES)


def _safe_model_identifier(value: str) -> bool:
    return re.fullmatch(_SAFE_MODEL_PATTERN, value) is not None and not _has_secret_prefix(value)


class _FrozenWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Week2CaseResult(_FrozenWireModel):
    """One sanitized case outcome; generated SQL and provider payloads are excluded."""

    case_id: str = Field(pattern=r"^[CPBS]2[0-9]{2}$")
    suite: SuiteName
    status: Week2CaseStatus
    result_score: Decimal | None = Field(default=None, ge=0, le=1)
    output_contract_conformant: bool | None = None
    query_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    finish_reason: NormalizedFinishReason | None = None
    output_truncated: bool = False
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_cny: Decimal = Field(default=Decimal("0"), ge=0)
    error_type: str | None = None
    expected_rejection: str | None = None
    observed_rejection: str | None = None

    @model_validator(mode="after")
    def _validate_case_contract(self) -> Week2CaseResult:
        is_safety = self.suite == "safety"
        if is_safety != self.case_id.startswith("S"):
            raise ValueError("case ID prefix must match suite")
        expected_prefix = {
            "core-v2": "C",
            "paraphrase": "P",
            "boundary": "B",
            "safety": "S",
        }[self.suite]
        if not self.case_id.startswith(expected_prefix):
            raise ValueError("case ID prefix must match suite")
        if self.output_truncated != (self.finish_reason == "length"):
            raise ValueError("output_truncated must match a length finish reason")
        if self.error_type == "pricing_failed" and self.estimated_cost_cny != 0:
            raise ValueError("unpriced calls cannot contain an estimated cost")
        if (
            self.error_type is not None
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_type) is None
        ):
            raise ValueError("error_type must be a stable safe category")
        for rejection in (self.expected_rejection, self.observed_rejection):
            if rejection is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rejection) is None:
                raise ValueError("rejection values must be stable safe categories")

        if is_safety:
            if self.status not in {"rejected", "rejection_mismatch", "unexpected_accept"}:
                raise ValueError("safety cases require a safety status")
            if self.result_score is not None or self.output_contract_conformant is not None:
                raise ValueError("safety cases cannot contain business scores")
            if self.query_id is not None or any(
                (
                    self.finish_reason is not None,
                    self.output_truncated,
                    self.latency_ms,
                    self.input_tokens,
                    self.output_tokens,
                    self.estimated_cost_cny,
                )
            ):
                raise ValueError("safety cases cannot contain model or execution telemetry")
            if self.expected_rejection is None:
                raise ValueError("safety cases require an expected rejection")
            if self.status == "rejected":
                if (
                    self.observed_rejection != self.expected_rejection
                    or self.error_type is not None
                ):
                    raise ValueError("successful rejection requires matching rules")
            elif self.status == "rejection_mismatch":
                if (
                    self.error_type != "safety_rejection_mismatch"
                    or self.observed_rejection is None
                    or self.observed_rejection == self.expected_rejection
                ):
                    raise ValueError("mismatched rejection requires differing rules")
            elif (
                self.error_type != "safety_unexpected_accept" or self.observed_rejection is not None
            ):
                raise ValueError("unexpected safety acceptance requires a stable error")
            return self

        if self.suite not in _EXECUTE_SUITES or self.status in {
            "rejected",
            "rejection_mismatch",
            "unexpected_accept",
        }:
            raise ValueError("execute cases require an execute status")
        if self.status in _SCORE_STATUSES:
            if self.result_score is None or self.output_contract_conformant is None:
                raise ValueError("scored cases require result and contract outcomes")
            if self.error_type is not None:
                raise ValueError("scored cases cannot contain an error type")
            if self.query_id is None:
                raise ValueError("scored cases require a query_id")
            expected_status: Week2CaseStatus
            if self.result_score == Decimal("1"):
                expected_status = (
                    "passed" if self.output_contract_conformant else "contract_violation"
                )
            else:
                expected_status = (
                    "wrong_answer"
                    if self.output_contract_conformant
                    else "wrong_answer_and_contract_violation"
                )
            if self.status != expected_status:
                raise ValueError("case status does not match score breakdown")
        else:
            if self.result_score is not None or self.output_contract_conformant is not None:
                raise ValueError("unscored errors cannot contain result outcomes")
            if self.status not in _ERROR_STATUSES or self.error_type is None:
                raise ValueError("execute errors require a stable error type")
            if self.status == "invalid_sql":
                if self.query_id is not None or self.error_type not in _INVALID_SQL_ERRORS:
                    raise ValueError("invalid SQL cases require an allowed pre-execution error")
            elif self.status == "execution_error" and (
                self.query_id is None or self.error_type not in _EXECUTION_ERRORS
            ):
                raise ValueError("execution errors require a query_id and allowed error")
        if self.expected_rejection is not None or self.observed_rejection is not None:
            raise ValueError("execute cases cannot expose policy rule details")
        return self


class Week2SuiteSummary(_FrozenWireModel):
    suite: SuiteName
    case_count: int = Field(gt=0)
    passed_count: int = Field(ge=0)
    result_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    output_contract_rate: Decimal | None = Field(default=None, ge=0, le=1)
    valid_sql_rate: Decimal | None = Field(default=None, ge=0, le=1)
    execution_success_rate: Decimal | None = Field(default=None, ge=0, le=1)
    safety_rejection_rate: Decimal | None = Field(default=None, ge=0, le=1)
    truncated_generation_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_summary_contract(self) -> Week2SuiteSummary:
        if self.passed_count > self.case_count:
            raise ValueError("passed_count cannot exceed case_count")
        if self.truncated_generation_count > self.case_count:
            raise ValueError("truncated count cannot exceed case_count")
        business_metrics = (
            self.result_accuracy,
            self.output_contract_rate,
            self.valid_sql_rate,
            self.execution_success_rate,
        )
        if self.suite == "safety":
            if any(metric is not None for metric in business_metrics):
                raise ValueError("safety summaries cannot contain business metrics")
            if self.safety_rejection_rate is None or self.truncated_generation_count:
                raise ValueError("safety summaries require only rejection metrics")
        elif any(metric is None for metric in business_metrics) or (
            self.safety_rejection_rate is not None
        ):
            raise ValueError("execute summaries require business metrics only")
        return self


class ComparisonReference(_FrozenWireModel):
    reference_run_id: str = Field(max_length=128, pattern=_SAFE_RUN_ID_PATTERN)
    reference_suite: Literal["core-v1"] = "core-v1"
    reference_protocol: str = Field(pattern=_SAFE_VERSION_PATTERN)
    reference_dataset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparison_scope: Literal["intent-aligned protocol comparison; not same-question accuracy"] = (
        "intent-aligned protocol comparison; not same-question accuracy"
    )
    comparison_status: Literal["comparable", "not_comparable"]

    @model_validator(mode="after")
    def _reject_secret_shaped_metadata(self) -> ComparisonReference:
        if _has_secret_prefix(self.reference_run_id) or _has_secret_prefix(self.reference_protocol):
            raise ValueError("comparison reference contains unsafe metadata")
        return self


@dataclass(frozen=True)
class DerivedWeek2Aggregates:
    result_accuracy: Decimal
    output_contract_rate: Decimal
    valid_sql_rate: Decimal
    execution_success_rate: Decimal
    safety_rejection_rate: Decimal
    truncated_generation_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_cny: Decimal
    cost_estimate_complete: bool
    unpriced_call_count: int
    suite_summaries: tuple[Week2SuiteSummary, ...]


def _rate(numerator: int | Decimal, denominator: int) -> Decimal:
    if denominator <= 0:
        raise ValueError("rate denominator must be positive")
    return Decimal(numerator) / Decimal(denominator)


def _derive_suite_summary(
    suite: SuiteName, cases: tuple[Week2CaseResult, ...]
) -> Week2SuiteSummary:
    suite_cases = tuple(case for case in cases if case.suite == suite)
    if not suite_cases:
        raise ValueError("active suite cannot be empty")
    if suite == "safety":
        rejected = sum(case.status == "rejected" for case in suite_cases)
        return Week2SuiteSummary(
            suite=suite,
            case_count=len(suite_cases),
            passed_count=rejected,
            safety_rejection_rate=_rate(rejected, len(suite_cases)),
        )
    scored = sum(case.status in _SCORE_STATUSES for case in suite_cases)
    return Week2SuiteSummary(
        suite=suite,
        case_count=len(suite_cases),
        passed_count=sum(case.status == "passed" for case in suite_cases),
        result_accuracy=sum(
            (case.result_score or Decimal("0") for case in suite_cases), Decimal("0")
        )
        / Decimal(len(suite_cases)),
        output_contract_rate=_rate(
            sum(case.output_contract_conformant is True for case in suite_cases),
            len(suite_cases),
        ),
        valid_sql_rate=_rate(
            sum(case.status != "invalid_sql" for case in suite_cases), len(suite_cases)
        ),
        execution_success_rate=_rate(scored, len(suite_cases)),
        truncated_generation_count=sum(case.output_truncated for case in suite_cases),
    )


def derive_week2_aggregates(
    cases: tuple[Week2CaseResult, ...],
) -> DerivedWeek2Aggregates:
    execute_cases = tuple(case for case in cases if case.suite != "safety")
    safety_cases = tuple(case for case in cases if case.suite == "safety")
    if len(execute_cases) != 50 or len(safety_cases) != 20:
        raise ValueError("Week 2 aggregates require the complete active suite")
    unpriced_call_count = sum(case.error_type == "pricing_failed" for case in execute_cases)
    return DerivedWeek2Aggregates(
        result_accuracy=sum(
            (case.result_score or Decimal("0") for case in execute_cases), Decimal("0")
        )
        / Decimal(len(execute_cases)),
        output_contract_rate=_rate(
            sum(case.output_contract_conformant is True for case in execute_cases),
            len(execute_cases),
        ),
        valid_sql_rate=_rate(
            sum(case.status != "invalid_sql" for case in execute_cases), len(execute_cases)
        ),
        execution_success_rate=_rate(
            sum(case.status in _SCORE_STATUSES for case in execute_cases),
            len(execute_cases),
        ),
        safety_rejection_rate=_rate(
            sum(case.status == "rejected" for case in safety_cases), len(safety_cases)
        ),
        truncated_generation_count=sum(case.output_truncated for case in execute_cases),
        total_input_tokens=sum(case.input_tokens for case in execute_cases),
        total_output_tokens=sum(case.output_tokens for case in execute_cases),
        total_cost_cny=sum((case.estimated_cost_cny for case in execute_cases), Decimal("0")),
        cost_estimate_complete=unpriced_call_count == 0,
        unpriced_call_count=unpriced_call_count,
        suite_summaries=tuple(
            _derive_suite_summary(suite, cases)
            for suite in ("core-v2", "paraphrase", "boundary", "safety")
        ),
    )


class Week2RunReport(_FrozenWireModel):
    protocol_version: Literal["week2-evaluation-v1"] = "week2-evaluation-v1"
    run_id: str = Field(max_length=128, pattern=_SAFE_RUN_ID_PATTERN)
    mode: Literal["fixture", "live"]
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(pattern=_SAFE_VERSION_PATTERN)
    requested_model: str = Field(pattern=_SAFE_MODEL_PATTERN)
    resolved_models: tuple[str, ...] = ()
    pricing_requested_model: str | None = None
    pricing_resolved_model: str | None = None
    pricing_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pricing_effective_date: date | None = None
    pricing_basis: str | None = None
    one_generation_one_execution: Literal[True] = True
    comparison_reference: ComparisonReference
    result_accuracy: Decimal = Field(ge=0, le=1)
    output_contract_rate: Decimal = Field(ge=0, le=1)
    valid_sql_rate: Decimal = Field(ge=0, le=1)
    execution_success_rate: Decimal = Field(ge=0, le=1)
    safety_rejection_rate: Decimal = Field(ge=0, le=1)
    truncated_generation_count: int = Field(ge=0)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_cost_cny: Decimal = Field(ge=0)
    cost_estimate_complete: bool
    unpriced_call_count: int = Field(ge=0, le=50)
    suite_summaries: tuple[Week2SuiteSummary, ...]
    cases: tuple[Week2CaseResult, ...]

    @model_validator(mode="after")
    def _validate_report_contract(self) -> Week2RunReport:
        if _has_secret_prefix(self.run_id) or _has_secret_prefix(self.prompt_version):
            raise ValueError("report contains unsafe identifier metadata")
        if tuple(summary.suite for summary in self.suite_summaries) != (
            "core-v2",
            "paraphrase",
            "boundary",
            "safety",
        ):
            raise ValueError("suite summaries must use the fixed active-suite order")
        expected_counts = {"core-v2": 20, "paraphrase": 20, "boundary": 10, "safety": 20}
        if {summary.suite: summary.case_count for summary in self.suite_summaries} != (
            expected_counts
        ):
            raise ValueError("Week 2 reports require exactly 70 active cases")
        case_ids = tuple(case.case_id for case in self.cases)
        if len(case_ids) != 70 or len(set(case_ids)) != 70:
            raise ValueError("Week 2 reports require 70 unique cases")
        expected_ids = tuple(
            [f"C2{number:02d}" for number in range(1, 21)]
            + [f"P2{number:02d}" for number in range(1, 21)]
            + [f"B2{number:02d}" for number in range(1, 11)]
            + [f"S2{number:02d}" for number in range(1, 21)]
        )
        if case_ids != expected_ids:
            raise ValueError("Week 2 case order is invalid")
        if tuple(sorted(set(self.resolved_models))) != self.resolved_models:
            raise ValueError("resolved_models must be a sorted unique tuple")
        if not _safe_model_identifier(self.requested_model) or any(
            not _safe_model_identifier(model) for model in self.resolved_models
        ):
            raise ValueError("report model identifiers must be safe")
        if self.mode == "live":
            if (
                self.pricing_requested_model is None
                or self.pricing_resolved_model is None
                or self.pricing_snapshot_sha256 is None
                or self.pricing_effective_date is None
                or not self.pricing_basis
            ):
                raise ValueError("live reports require pricing metadata")
            if self.pricing_requested_model != self.requested_model:
                raise ValueError("live report pricing must match the requested model")
            if not _safe_model_identifier(
                self.pricing_requested_model
            ) or not _safe_model_identifier(self.pricing_resolved_model):
                raise ValueError("report pricing model identifiers must be safe")
            if WEEK2_FIXTURE_MODEL in {
                self.requested_model,
                self.pricing_requested_model,
                self.pricing_resolved_model,
            }:
                raise ValueError("live reports cannot use fixture model identities")
        elif any(
            value is not None
            for value in (
                self.pricing_requested_model,
                self.pricing_resolved_model,
                self.pricing_snapshot_sha256,
                self.pricing_effective_date,
                self.pricing_basis,
            )
        ):
            raise ValueError("fixture reports cannot contain pricing metadata")
        if self.mode == "fixture":
            if self.requested_model != WEEK2_FIXTURE_MODEL or self.resolved_models != (
                WEEK2_FIXTURE_MODEL,
            ):
                raise ValueError("fixture reports require the fixed fixture model identity")
            if self.comparison_reference.comparison_status != "not_comparable":
                raise ValueError("fixture reports cannot be historically comparable")
            execute_cases = tuple(case for case in self.cases if case.suite != "safety")
            if any(
                case.finish_reason is not None
                or case.output_truncated
                or case.latency_ms != 0
                or case.input_tokens != 0
                or case.output_tokens != 0
                or case.estimated_cost_cny != 0
                for case in execute_cases
            ):
                raise ValueError("fixture reports cannot contain live-call telemetry")
        if self.pricing_basis not in {None, WEEK2_PRICING_BASIS}:
            raise ValueError("pricing_basis must use the pinned Week 2 basis")
        derived = derive_week2_aggregates(self.cases)
        if self.suite_summaries != derived.suite_summaries or any(
            (
                self.result_accuracy != derived.result_accuracy,
                self.output_contract_rate != derived.output_contract_rate,
                self.valid_sql_rate != derived.valid_sql_rate,
                self.execution_success_rate != derived.execution_success_rate,
                self.safety_rejection_rate != derived.safety_rejection_rate,
                self.truncated_generation_count != derived.truncated_generation_count,
                self.total_input_tokens != derived.total_input_tokens,
                self.total_output_tokens != derived.total_output_tokens,
                self.total_cost_cny != derived.total_cost_cny,
                self.cost_estimate_complete != derived.cost_estimate_complete,
                self.unpriced_call_count != derived.unpriced_call_count,
            )
        ):
            raise ValueError("Week 2 aggregates must be derived from cases")
        if self.mode == "fixture" and (
            not self.cost_estimate_complete or self.unpriced_call_count != 0
        ):
            raise ValueError("fixture reports require complete zero-cost estimates")
        if (
            self.comparison_reference.comparison_status == "comparable"
            and not self.cost_estimate_complete
        ):
            raise ValueError("incomplete costs cannot be historically comparable")
        return self
