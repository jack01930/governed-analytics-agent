"""Model-independent contracts for one-pass SQL generation."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from governed_analytics.evals.models import GeneratedSql


class SqlGenerationRequest(BaseModel):
    """Immutable, fully specified input to a SQL generator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: str = Field(pattern=r"^G[0-9]{3}$")
    question: str = Field(min_length=1)
    schema_context: str = Field(min_length=1)
    metric_context: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_nonblank_text(self) -> SqlGenerationRequest:
        if not (
            self.question.strip()
            and self.schema_context.strip()
            and self.metric_context.strip()
        ):
            raise ValueError("question, schema_context, and metric_context must not be blank")
        return self


class SqlGenerator(Protocol):
    """A one-request SQL generator; implementations must not repair or retry."""

    async def generate(self, request: SqlGenerationRequest) -> GeneratedSql: ...
