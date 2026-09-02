"""Immutable public contract for governed business metrics."""

import re
from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class MetricDefinition(BaseModel):
    """One versioned, metadata-only business metric definition."""

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
    unit: Literal["cny", "count", "ratio"]
    version: str
    valid_from: datetime

    @field_validator(
        "metric_id",
        "name_zh",
        "name_en",
        "description",
        "expression_sql",
        "time_field",
        "version",
    )
    @classmethod
    def validate_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("string fields must be non-empty")
        return value

    @field_validator("metric_id")
    @classmethod
    def validate_metric_id(cls, value: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is None:
            raise ValueError("metric_id must be snake_case")
        return value

    @field_validator("version")
    @classmethod
    def validate_semantic_version(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError("version must use semantic version format")
        return value

    @field_validator("default_filters", "dimensions", "source_tables")
    @classmethod
    def validate_ordered_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("tuple fields cannot contain empty strings")
        if len(value) != len(set(value)):
            raise ValueError("tuple fields must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        if not self.dimensions:
            raise ValueError("dimensions must be non-empty")
        if not self.source_tables:
            raise ValueError("source_tables must be non-empty")
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() != timedelta(0):
            raise ValueError("valid_from must be UTC-aware with offset +00:00")
        return self
