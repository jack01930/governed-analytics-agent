"""Strict, filtered HTTP response contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from governed_analytics.agent.contracts import (
    ActionType,
    AgentFinishReason,
    FinalStatus,
    RunLifecycleStatus,
    StopReason,
)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnalysisCreateRequest(_WireModel):
    query: str = Field(min_length=1, max_length=4000)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class AnalysisCreateResponse(_WireModel):
    run_id: str
    lifecycle_status: Literal["queued"]
    status_url: str
    events_url: str
    trace_url: str


class SafeEvidenceResponse(_WireModel):
    evidence_id: str
    observation_id: str
    hypothesis_id: str
    contract_id: str
    query_id: str
    claim_key: str
    dimensions: tuple[tuple[str, str], ...] = ()
    stance: Literal["supports", "refutes"]
    numeric_value: Decimal | None = None
    unit: str | None = None
    limitations: tuple[str, ...] = ()


class AnalysisStatusResponse(_WireModel):
    run_id: str
    lifecycle_status: RunLifecycleStatus
    final_status: FinalStatus | None = None
    answer: str | None = None
    evidence: tuple[SafeEvidenceResponse, ...] = ()
    limitations: tuple[str, ...] = ()
    stop_reason: StopReason | None = None

    @model_validator(mode="after")
    def validate_terminal_projection(self) -> AnalysisStatusResponse:
        terminal_values = (self.final_status, self.answer, self.stop_reason)
        if self.lifecycle_status is RunLifecycleStatus.TERMINAL:
            if any(value is None for value in terminal_values):
                raise ValueError("terminal status requires complete public output")
        elif (
            any(value is not None for value in terminal_values) or self.evidence or self.limitations
        ):
            raise ValueError("nonterminal status cannot expose terminal output")
        return self


class SafeNodeTrace(_WireModel):
    node: str
    duration_ms: int = Field(ge=0)
    outcome: Literal["completed", "failed", "skipped"]


class SafeModelCallTrace(_WireModel):
    purpose: Literal["behavior", "plan", "action", "synthesis", "repair"]
    provider_model: str
    outcome: Literal["completed", "failed", "cancelled"]
    safe_error: str | None = None
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    finish_reason: AgentFinishReason | None
    output_truncated: bool
    estimated_cost_cny: Decimal = Field(ge=0)


class SafeToolTrace(_WireModel):
    tool_name: ActionType
    purpose: str
    safe_arguments: tuple[tuple[str, object], ...]
    query_id: str | None = None
    columns: tuple[str, ...] = ()
    row_count: int | None = Field(default=None, ge=0)
    possibly_truncated: bool = False
    safe_error: str | None = None


class TraceResponse(_WireModel):
    run_id: str
    snapshot_complete: bool
    nodes: tuple[SafeNodeTrace, ...]
    model_calls: tuple[SafeModelCallTrace, ...]
    tool_calls: tuple[SafeToolTrace, ...]
    evidence_gaps: tuple[str, ...]
    repair_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    committed_cost_cny: Decimal = Field(ge=0)
    stop_reason: StopReason | None


class HealthResponse(_WireModel):
    status: Literal["ok"] = "ok"


class RequestValidationErrorResponse(_WireModel):
    detail: Literal["request_validation_failed"] = "request_validation_failed"


__all__ = [
    "AnalysisCreateRequest",
    "AnalysisCreateResponse",
    "AnalysisStatusResponse",
    "HealthResponse",
    "RequestValidationErrorResponse",
    "SafeEvidenceResponse",
    "SafeModelCallTrace",
    "SafeNodeTrace",
    "SafeToolTrace",
    "TraceResponse",
]
