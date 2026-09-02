"""Immutable wire models shared by baseline evaluation components."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class GeneratedSql(_FrozenWireModel):
    sql: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    provider_model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


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
    run_id: str = Field(min_length=1)
    mode: Literal["fixture", "live"]
    dataset_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    resolved_models: tuple[str, ...] = ()
    result_accuracy: Decimal = Field(ge=0, le=1)
    valid_sql_rate: Decimal = Field(ge=0, le=1)
    execution_success_rate: Decimal = Field(ge=0, le=1)
    total_cost_cny: Decimal = Field(ge=0)
    cases: tuple[BaselineCaseResult, ...]

    @model_validator(mode="after")
    def _validate_resolved_models(self) -> BaselineRunReport:
        if tuple(sorted(set(self.resolved_models))) != self.resolved_models:
            raise ValueError("resolved_models must be a sorted, unique tuple")
        if any(not model for model in self.resolved_models):
            raise ValueError("resolved_models cannot contain empty values")
        return self
