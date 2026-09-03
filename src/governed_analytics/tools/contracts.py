"""Immutable request/response contracts and stable tool error codes."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    SQL_REJECTED = "sql_rejected"
    QUERY_TIMEOUT = "query_timeout"
    EXECUTION_FAILED = "execution_failed"
    SENSITIVE_RESULT_BLOCKED = "sensitive_result_blocked"


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str = Field(min_length=1, max_length=128)
    retryable: bool = False


T = TypeVar("T")

type SqlParameterValue = str | int | float | bool | None

_MAX_SQL_PARAMETER_ABS = 10**18


class ToolResponse[T](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    data: T | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> ToolResponse[T]:
        if self.ok and (self.data is None or self.error is not None):
            raise ValueError("successful response requires data and no error")
        if not self.ok and (self.error is None or self.data is not None):
            raise ValueError("failed response requires error and no data")
        return self


class SchemaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_names: tuple[str, ...] = ()

    @field_validator("table_names")
    @classmethod
    def _validate_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not item or not item.replace("_", "").isalnum() or item[0].isdigit() for item in value
        ):
            raise ValueError("table_names must be unique snake_case identifiers")
        return value


class MetricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class ProfileFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    column_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: str | int | float | bool

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> object:
        if type(value) not in {str, int, float, bool}:
            raise ValueError("profile filter values must be strict JSON scalars")
        if isinstance(value, str) and len(value) > 256:
            raise ValueError("profile filter strings must be at most 256 characters")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("profile filter numbers must be finite")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and abs(value) > _MAX_SQL_PARAMETER_ABS
        ):
            raise ValueError("profile filter numbers must be bounded")
        return value


type ProfileOperation = Literal[
    "time_range",
    "numeric_summary",
    "null_summary",
    "distinct_values",
    "top_values",
]


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    column_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    operation: ProfileOperation = "distinct_values"
    filters: tuple[ProfileFilter, ...] = ()
    time_column: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    start_at: datetime | None = None
    end_at: datetime | None = None
    limit: int = Field(default=50, ge=1, le=50)

    @model_validator(mode="after")
    def _validate_window(self) -> ProfileRequest:
        if len({item.column_name for item in self.filters}) != len(self.filters):
            raise ValueError("profile filters must use unique columns")
        has_window = self.start_at is not None or self.end_at is not None
        if has_window and (
            self.start_at is None or self.end_at is None or self.time_column is None
        ):
            raise ValueError("time windows require a column and both bounds")
        if not has_window and self.time_column is not None:
            raise ValueError("time_column requires both time-window bounds")
        if self.start_at is not None and self.end_at is not None:
            if (
                self.start_at.tzinfo is None
                or self.start_at.utcoffset() is None
                or self.end_at.tzinfo is None
                or self.end_at.utcoffset() is None
            ):
                raise ValueError("time-window bounds must be timezone-aware")
            if self.start_at >= self.end_at:
                raise ValueError("time window must be half-open and ordered")
        return self


class ExecuteSqlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sql: str = Field(min_length=1, max_length=100_000)
    parameters: Mapping[str, SqlParameterValue] = Field(default_factory=dict)

    @field_validator("sql")
    @classmethod
    def _reject_blank_sql(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sql must not be blank")
        return value

    @field_validator("parameters", mode="before")
    @classmethod
    def _validate_parameter_input(cls, value: object) -> object:
        if not isinstance(value, Mapping) or len(value) > 64:
            raise ValueError("SQL parameters must use bounded safe names")
        for name, item in value.items():
            if type(name) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
                raise ValueError("SQL parameters must use bounded safe names")
            if type(item) not in {str, int, float, bool, type(None)}:
                raise ValueError("SQL parameter values must be strict JSON scalars")
            if isinstance(item, str) and len(item) > 4096:
                raise ValueError("SQL parameter strings must be bounded")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("SQL parameter numbers must be finite")
            if (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and abs(item) > _MAX_SQL_PARAMETER_ABS
            ):
                raise ValueError("SQL parameter numbers must be bounded")
        return dict(value)

    @field_validator("parameters")
    @classmethod
    def _freeze_parameters(
        cls, value: Mapping[str, SqlParameterValue]
    ) -> Mapping[str, SqlParameterValue]:
        return MappingProxyType(dict(value))

    @field_serializer("parameters")
    def _serialize_parameters(
        self, value: Mapping[str, SqlParameterValue]
    ) -> dict[str, SqlParameterValue]:
        return dict(value)


class ColumnInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    data_type: str
    nullable: bool
    allowed_values: tuple[str, ...] = ()


class ForeignKeyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column_name: str
    target_table: str
    target_column: str


class TableInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...] = ()
    schema_version: Literal["0001"] = "0001"


class MetricInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str
    name_zh: str
    name_en: str
    description: str
    expression_sql: str
    time_field: str
    default_filters: tuple[str, ...]
    dimensions: tuple[str, ...]
    source_tables: tuple[str, ...]
    unit: str
    version: str


class ProfileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    table_name: str
    column_name: str
    operation: ProfileOperation
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int = Field(ge=0, le=50)
    value_limit: int = Field(default=50, ge=1, le=50)
    possibly_truncated: bool = False

    @model_validator(mode="after")
    def _validate_rows(self) -> ProfileResult:
        if self.row_count != len(self.rows):
            raise ValueError("row_count must match rows")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("profile columns must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("profile row shape must match columns")
        if self.possibly_truncated and self.row_count != self.value_limit:
            raise ValueError("only a full value page may be possibly truncated")
        return self


class ExecuteSqlResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int = Field(ge=0, le=500)
    row_limit: Literal[500] = 500
    possibly_truncated: bool = False

    @model_validator(mode="after")
    def _validate_rows(self) -> ExecuteSqlResult:
        if self.row_count != len(self.rows):
            raise ValueError("row_count must match rows")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("result columns must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("row shape must match columns")
        if self.possibly_truncated and self.row_count != self.row_limit:
            raise ValueError("only a full result page may be possibly truncated")
        return self


QueryResult = ExecuteSqlResult
