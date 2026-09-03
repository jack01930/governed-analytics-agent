"""Immutable wire models shared by baseline evaluation components."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_EXPECTED_EVALUATION_CASE_IDS = tuple(f"G{number:03d}" for number in range(1, 21))

type NormalizedFinishReason = Literal[
    "stop", "length", "content_filter", "tool_calls", "other", "unknown"
]


class _FrozenWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldenCase(_FrozenWireModel):
    case_id: str = Field(pattern=r"^G[0-9]{3}$")
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    oracle_sql_path: Path
    comparison: Literal["scalar", "table", "top_k", "boolean"]
    key_columns: tuple[str, ...] = ()
    numeric_columns: tuple[str, ...] = ()
    absolute_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)
    relative_tolerance: Decimal = Field(default=Decimal("0.000001"), ge=0)

    @model_validator(mode="after")
    def _validate_comparison_metadata(self) -> GoldenCase:
        if not self.question.strip() or not self.category.strip():
            raise ValueError("question and category must not be blank")
        if len(set(self.key_columns)) != len(self.key_columns):
            raise ValueError("key_columns must not contain duplicates")
        if len(set(self.numeric_columns)) != len(self.numeric_columns):
            raise ValueError("numeric_columns must not contain duplicates")
        if set(self.key_columns) & set(self.numeric_columns):
            raise ValueError("key_columns and numeric_columns must not overlap")
        if self.comparison == "top_k" and not self.key_columns:
            raise ValueError("top_k cases require key_columns")
        if self.comparison in {"scalar", "boolean"} and self.key_columns:
            raise ValueError("scalar and boolean cases cannot declare key_columns")
        if self.comparison == "boolean" and self.numeric_columns:
            raise ValueError("boolean cases cannot declare numeric_columns")
        return self


class BaselineCaseResult(_FrozenWireModel):
    case_id: str = Field(pattern=r"^G[0-9]{3}$")
    generated_sql: str | None
    status: Literal["passed", "wrong_answer", "invalid_sql", "execution_error"]
    score: Decimal = Field(ge=0, le=1)
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_cny: Decimal = Field(ge=0)
    error_type: str | None = None

    @model_validator(mode="after")
    def _validate_status_contract(self) -> BaselineCaseResult:
        if self.status == "passed" and self.score != Decimal("1"):
            raise ValueError("passed cases must have score 1")
        if self.status == "wrong_answer" and self.score >= Decimal("1"):
            raise ValueError("wrong_answer cases must have score below 1")
        is_error = self.status in {"invalid_sql", "execution_error"}
        if is_error and self.score != Decimal("0"):
            raise ValueError("error cases must have score 0")
        if is_error and self.error_type is None:
            raise ValueError("error cases require an error_type category")
        if not is_error and self.error_type is not None:
            raise ValueError("non-error cases cannot have an error_type")
        if (
            self.error_type is not None
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_type) is None
        ):
            raise ValueError("error_type must be a stable safe category")
        return self


class GeneratedSql(_FrozenWireModel):
    sql: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    provider_model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    finish_reason: NormalizedFinishReason | None = None
    output_truncated: bool = False

    @model_validator(mode="after")
    def _validate_generation_telemetry(self) -> GeneratedSql:
        if self.output_truncated != (self.finish_reason == "length"):
            raise ValueError("output_truncated must match a length finish reason")
        return self


class QueryResult(_FrozenWireModel):
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]

    @model_validator(mode="after")
    def _validate_row_shapes(self) -> QueryResult:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must not contain duplicates")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("each row must match the columns length")
        return self


class BaselineRunReport(_FrozenWireModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal["fixture", "live"]
    dataset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    resolved_models: tuple[str, ...] = ()
    pricing_effective_date: date | None = None
    pricing_basis: str | None = None
    result_accuracy: Decimal = Field(ge=0, le=1)
    valid_sql_rate: Decimal = Field(ge=0, le=1)
    execution_success_rate: Decimal = Field(ge=0, le=1)
    total_cost_cny: Decimal = Field(ge=0)
    cases: tuple[BaselineCaseResult, ...]

    @model_validator(mode="after")
    def _validate_report_contract(self) -> BaselineRunReport:
        if self.model != self.requested_model:
            raise ValueError("model must equal requested_model")
        if tuple(sorted(set(self.resolved_models))) != self.resolved_models:
            raise ValueError("resolved_models must be a sorted, unique tuple")
        if any(not model for model in self.resolved_models):
            raise ValueError("resolved_models cannot contain empty values")
        if self.mode == "live":
            if self.pricing_effective_date is None or self.pricing_basis is None:
                raise ValueError("live reports require pricing metadata")
            if not self.pricing_basis.strip():
                raise ValueError("pricing_basis must not be blank")
        elif self.pricing_effective_date is not None or self.pricing_basis is not None:
            raise ValueError("fixture reports cannot contain pricing metadata")
        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("baseline reports cannot contain duplicate case IDs")
        if case_ids != _EXPECTED_EVALUATION_CASE_IDS:
            raise ValueError("baseline reports require exactly 20 ordered G001 through G020 cases")
        return self
