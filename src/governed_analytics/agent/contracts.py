"""Frozen, public domain contracts for governed agent execution."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from governed_analytics.tools.contracts import MetricInfo, TableInfo

type ModelPurpose = Literal["behavior", "plan", "action", "synthesis", "repair"]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
type FrozenJsonObject = Mapping[str, JsonValue]

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)]
NonBlankText = Annotated[str, Field(min_length=1, max_length=4096)]
QueryId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


class BehaviorAction(StrEnum):
    EXECUTE = "execute"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    UNSUPPORTED = "unsupported"


class BehaviorReasonCode(StrEnum):
    READY = "ready"
    MISSING_METRIC = "missing_metric"
    MISSING_TIME_WINDOW = "missing_time_window"
    MISSING_COMPARISON_WINDOW = "missing_comparison_window"
    AMBIGUOUS_METRIC = "ambiguous_metric"
    UNSAFE_REQUEST = "unsafe_request"
    SENSITIVE_DATA_REQUEST = "sensitive_data_request"
    UNSUPPORTED_ANALYSIS = "unsupported_analysis"
    UNSUPPORTED_DATA_DOMAIN = "unsupported_data_domain"


class AnalysisType(StrEnum):
    SIMPLE = "simple"
    COMPARISON = "comparison"
    ATTRIBUTION = "attribution"


class ActionType(StrEnum):
    METRIC_LOOKUP = "metric_lookup"
    SCHEMA_LOOKUP = "schema_lookup"
    PROFILE = "profile"
    EXECUTE_SQL = "execute_sql"


class ResultShape(StrEnum):
    SCALAR = "scalar"
    SINGLE_ROW = "single_row"
    TABLE = "table"
    TOP_K = "top_k"


class FinalStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    CLARIFICATION_REQUIRED = "clarification_required"
    REFUSED = "refused"
    UNSUPPORTED = "unsupported"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_BLOCKED = "policy_blocked"
    MODEL_UNAVAILABLE = "model_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INTERNAL_ERROR = "internal_error"


class RunLifecycleStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    TERMINAL = "terminal"


class AgentFinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"
    OTHER = "other"
    UNKNOWN = "unknown"


class AgentModelErrorCategory(StrEnum):
    """Stable allowlist for every safe Agent model-invocation failure category."""

    INVALID_REQUEST = "invalid_request"
    SCHEMA_IDENTITY_MISMATCH = "schema_identity_mismatch"
    PROVIDER_CALL_FAILED = "provider_call_failed"
    PROVIDER_HTTP_4XX = "provider_http_4xx"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_HTTP_5XX = "provider_http_5xx"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CONNECTION_ERROR = "provider_connection_error"
    MISSING_CONTENT = "missing_content"
    INVALID_CONTENT_TYPE = "invalid_content_type"
    INVALID_JSON = "invalid_json"
    INVALID_STRUCTURE = "invalid_structure"
    MISSING_USAGE = "missing_usage"
    INVALID_USAGE = "invalid_usage"
    MISSING_MODEL = "missing_model"
    FIXTURE_SCRIPT_MISMATCH = "fixture_script_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    BUDGET_RESERVATION_FAILED = "budget_reservation_failed"
    REPAIR_BLOCKED = "repair_blocked"
    REPAIR_FAILED = "repair_failed"
    ACCOUNTING_CONTRACT_FAILED = "accounting_contract_failed"
    CANCELLED = "cancelled"


class StopReason(StrEnum):
    ANSWER_COMPLETE = "answer_complete"
    PREMISE_NOT_MET = "premise_not_met"
    EVIDENCE_PARTIAL = "evidence_partial"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    UNSUPPORTED_ANALYSIS = "unsupported_analysis"
    UNSUPPORTED_DATA_DOMAIN = "unsupported_data_domain"
    UNSAFE_REQUEST = "unsafe_request"
    SENSITIVE_DATA_REQUEST = "sensitive_data_request"
    SQL_POLICY_REJECTED = "sql_policy_rejected"
    SENSITIVE_RESULT_BLOCKED = "sensitive_result_blocked"
    COST_SOFT_CAP = "cost_soft_cap"
    COST_HARD_CAP = "cost_hard_cap"
    LLM_CALL_LIMIT = "llm_call_limit"
    TOOL_CALL_LIMIT = "tool_call_limit"
    EXECUTE_LIMIT = "execute_limit"
    PROFILE_LIMIT = "profile_limit"
    ANALYSIS_LOOP_LIMIT = "analysis_loop_limit"
    SQL_TIMEOUT = "sql_timeout"
    TASK_TIMEOUT = "task_timeout"
    RESULT_TRUNCATED = "result_truncated"
    DATABASE_ERROR = "database_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    PLAN_INVALID = "plan_invalid"
    ANSWER_CONTRACT_UNMET = "answer_contract_unmet"
    REPAIR_FAILED = "repair_failed"
    INTERNAL_ERROR = "internal_error"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _json_datetime(value: datetime) -> str:
    rendered = value.isoformat()
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


def _normalize_json(value: object) -> JsonValue:
    """Normalize supported Pydantic JSON-mode values without admitting arbitrary objects."""
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON decimals must be finite")
        return str(value)
    if isinstance(value, datetime):
        return _json_datetime(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalize_json(value.value)
    if isinstance(value, BaseModel):
        return _normalize_json(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            normalized[key] = _normalize_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_json(item) for item in value)
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _normalize_and_freeze_json(value: object) -> JsonValue:
    return _freeze_json(_normalize_json(value))


def _normalize_and_freeze_object(value: object) -> FrozenJsonObject:
    normalized = _normalize_and_freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise ValueError("value must be a JSON object")
    return normalized


def _thaw_json(value: JsonValue) -> JsonScalar | list[object] | dict[str, object]:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _key_tokens(key: str) -> tuple[str, ...]:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", snake_case).strip("_").lower()
    return tuple(token for token in normalized.split("_") if token)


def _is_evaluation_only_key(key: str) -> bool:
    tokens = _key_tokens(key)
    return any(token in {"oracle", "expected", "scorer"} for token in tokens)


_FINAL_SUMMARY_FORBIDDEN_TOKENS = frozenset(
    {
        "apikey",
        "authorization",
        "binding",
        "bindings",
        "credential",
        "credentials",
        "endpoint",
        "exception",
        "param",
        "parameter",
        "parameters",
        "params",
        "password",
        "payload",
        "prompt",
        "raw",
        "record",
        "records",
        "row",
        "rows",
        "secret",
        "sql",
        "statement",
        "token",
        "traceback",
        "uri",
        "url",
    }
)


def _is_final_summary_forbidden_key(key: str) -> bool:
    tokens = _key_tokens(key)
    compact = "".join(tokens)
    return (
        _is_evaluation_only_key(key)
        or compact in {"apikey", "baseurl", "rawerror", "databaseerror"}
        or any(token in _FINAL_SUMMARY_FORBIDDEN_TOKENS for token in tokens)
    )


def _json_has_forbidden_key(
    value: JsonValue,
    *,
    predicate: Callable[[str], bool],
) -> bool:
    if isinstance(value, Mapping):
        return any(
            predicate(key) or _json_has_forbidden_key(item, predicate=predicate)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return any(_json_has_forbidden_key(item, predicate=predicate) for item in value)
    return False


FrozenJsonValue = Annotated[
    JsonValue,
    BeforeValidator(_normalize_and_freeze_json),
    AfterValidator(_freeze_json),
]
FrozenJsonObjectValue = Annotated[
    FrozenJsonObject,
    BeforeValidator(_normalize_and_freeze_object),
    AfterValidator(_normalize_and_freeze_object),
]


def _require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


class TimeWindow(_FrozenModel):
    label: Identifier
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _validate_window(self) -> TimeWindow:
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


class BehaviorDecision(_FrozenModel):
    """Route with matching action/reason_code and unique missing_fields.

    execute requires ready; clarify requires missing_metric, missing_time_window,
    missing_comparison_window or ambiguous_metric and nonempty missing_fields;
    refuse requires unsafe_request or sensitive_data_request; unsupported requires
    unsupported_analysis or unsupported_data_domain. Only clarify has missing_fields.
    Allowed missing_fields: metric, time_window, previous_window, current_window,
    comparison_window. Do not infer omitted requirements from an example.
    """

    action: BehaviorAction
    reason_code: BehaviorReasonCode
    missing_fields: tuple[Identifier, ...] = ()
    user_message: NonBlankText

    @field_validator("user_message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        return _require_nonblank(value)

    @model_validator(mode="after")
    def _validate_decision(self) -> BehaviorDecision:
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("missing_fields must be unique")
        clarify_reasons = {
            BehaviorReasonCode.MISSING_METRIC,
            BehaviorReasonCode.MISSING_TIME_WINDOW,
            BehaviorReasonCode.MISSING_COMPARISON_WINDOW,
            BehaviorReasonCode.AMBIGUOUS_METRIC,
        }
        expected_reasons = {
            BehaviorAction.EXECUTE: {BehaviorReasonCode.READY},
            BehaviorAction.CLARIFY: clarify_reasons,
            BehaviorAction.REFUSE: {
                BehaviorReasonCode.UNSAFE_REQUEST,
                BehaviorReasonCode.SENSITIVE_DATA_REQUEST,
            },
            BehaviorAction.UNSUPPORTED: {
                BehaviorReasonCode.UNSUPPORTED_ANALYSIS,
                BehaviorReasonCode.UNSUPPORTED_DATA_DOMAIN,
            },
        }
        if self.reason_code not in expected_reasons[self.action]:
            raise ValueError("reason_code does not match behavior action")
        if self.action is BehaviorAction.CLARIFY and not self.missing_fields:
            raise ValueError("clarification requires missing fields")
        if self.action is not BehaviorAction.CLARIFY and self.missing_fields:
            raise ValueError("only clarification may identify missing fields")
        return self


class Hypothesis(_FrozenModel):
    hypothesis_id: Identifier
    kind: Literal["metric_value", "confirm_decline", "dimension_contribution"]
    dimension: Identifier | None = None
    status: Literal["pending", "supported", "refuted"] = "pending"

    @model_validator(mode="after")
    def _validate_dimension(self) -> Hypothesis:
        needs_dimension = self.kind == "dimension_contribution"
        if needs_dimension != (self.dimension is not None):
            raise ValueError("only dimension-contribution hypotheses require a dimension")
        return self


class SortKey(_FrozenModel):
    column: Identifier
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"] = "last"


class ColumnContract(_FrozenModel):
    name: Identifier
    data_type: Literal["string", "integer", "decimal", "boolean", "date", "datetime"]
    role: Literal["dimension", "metric", "period", "identifier"]
    nullable: bool = False
    unit: Identifier | None = None

    @model_validator(mode="after")
    def _validate_unit(self) -> ColumnContract:
        requires_unit = self.role == "metric" and self.data_type in {
            "integer",
            "decimal",
        }
        if requires_unit and self.unit is None:
            raise ValueError("numeric metric columns require unit")
        if not requires_unit and self.unit is not None:
            raise ValueError("only numeric metric columns may carry unit")
        return self


class TypedMetricPlan(_FrozenModel):
    plan_id: Identifier
    revision: Annotated[int, Field(ge=1)]
    metric_id: Identifier
    metric_version: Identifier
    analysis_type: AnalysisType
    windows: tuple[TimeWindow, ...]
    grain: tuple[Identifier, ...] = ()
    dimensions: tuple[Identifier, ...] = ()
    filters: tuple[NonBlankText, ...] = ()
    numerator: NonBlankText | None = None
    denominator: NonBlankText | None = None
    null_policy: NonBlankText
    zero_denominator_policy: NonBlankText
    fill_policy: NonBlankText
    sort: tuple[SortKey, ...] = ()
    top_k: Annotated[int, Field(gt=0)] | None = None
    tie_break: tuple[Identifier, ...] = ()
    hypotheses: tuple[Hypothesis, ...]

    @model_validator(mode="after")
    def _validate_plan(self) -> TypedMetricPlan:
        sequences = (self.grain, self.dimensions, self.filters, self.tie_break)
        if any(len(items) != len(set(items)) for items in sequences):
            raise ValueError("plan sequences must contain unique values")
        if not self.windows or len({item.label for item in self.windows}) != len(self.windows):
            raise ValueError("plan windows must be nonempty and uniquely labelled")
        if not self.hypotheses or len({item.hypothesis_id for item in self.hypotheses}) != len(
            self.hypotheses
        ):
            raise ValueError("plan hypotheses must be nonempty and unique")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("numerator and denominator must be specified together")
        if self.analysis_type is AnalysisType.ATTRIBUTION and not self.dimensions:
            raise ValueError("attribution requires dimensions")
        if self.top_k is not None and not self.sort:
            raise ValueError("top_k requires deterministic sorting")
        return self


class ObservationContract(_FrozenModel):
    contract_id: Identifier
    hypothesis_id: Identifier
    columns: tuple[ColumnContract, ...]
    shape: ResultShape
    min_rows: NonNegativeInt
    max_rows: NonNegativeInt
    key_columns: tuple[Identifier, ...] = ()
    order_by: tuple[SortKey, ...] = ()
    limit: Annotated[int, Field(gt=0)] | None = None
    allow_truncation: bool = False

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @model_validator(mode="after")
    def _validate_contract(self) -> ObservationContract:
        if not self.columns or len(self.column_names) != len(set(self.column_names)):
            raise ValueError("contract columns must be nonempty and unique")
        if self.min_rows > self.max_rows:
            raise ValueError("min_rows cannot exceed max_rows")
        if self.shape is ResultShape.SCALAR and (self.min_rows, self.max_rows) != (1, 1):
            raise ValueError("scalar contracts require exactly one row")
        if self.shape is ResultShape.SINGLE_ROW and self.max_rows != 1:
            raise ValueError("single-row contracts permit at most one row")
        if len(self.key_columns) != len(set(self.key_columns)) or not set(
            self.key_columns
        ).issubset(self.column_names):
            raise ValueError("key columns must be unique contract columns")
        order_columns = tuple(item.column for item in self.order_by)
        if len(order_columns) != len(set(order_columns)) or not set(order_columns).issubset(
            self.column_names
        ):
            raise ValueError("order columns must be unique contract columns")
        if self.limit is not None and self.limit > self.max_rows:
            raise ValueError("limit cannot exceed max_rows")
        return self


class AnswerContract(_FrozenModel):
    answer_contract_id: Identifier
    required_hypotheses: tuple[Identifier, ...]
    observation_contracts: tuple[ObservationContract, ...]

    @model_validator(mode="after")
    def _validate_contracts(self) -> AnswerContract:
        if not self.required_hypotheses or len(self.required_hypotheses) != len(
            set(self.required_hypotheses)
        ):
            raise ValueError("required hypotheses must be nonempty and unique")
        contract_ids = tuple(item.contract_id for item in self.observation_contracts)
        if not contract_ids or len(contract_ids) != len(set(contract_ids)):
            raise ValueError("observation contracts must be nonempty and uniquely identified")
        if not {item.hypothesis_id for item in self.observation_contracts}.issubset(
            self.required_hypotheses
        ):
            raise ValueError("observation contracts must reference required hypotheses")
        if {item.hypothesis_id for item in self.observation_contracts} != set(
            self.required_hypotheses
        ):
            raise ValueError("every required hypothesis must have an observation contract")
        return self

    def contract(self, contract_id: str) -> ObservationContract:
        matches = tuple(
            item for item in self.observation_contracts if item.contract_id == contract_id
        )
        if len(matches) != 1:
            raise KeyError(contract_id)
        return matches[0]


class AgentAction(_FrozenModel):
    action_type: ActionType
    purpose: NonBlankText
    arguments: FrozenJsonObjectValue
    hypothesis_id: Identifier | None = None
    contract_id: Identifier | None = None
    expected_evidence: NonBlankText

    @field_serializer("arguments")
    def _serialize_arguments(self, value: FrozenJsonObject) -> dict[str, object]:
        return cast(dict[str, object], _thaw_json(value))

    @model_validator(mode="after")
    def _validate_action(self) -> AgentAction:
        _require_nonblank(self.purpose)
        _require_nonblank(self.expected_evidence)
        if self.action_type is ActionType.EXECUTE_SQL and (
            self.contract_id is None or self.hypothesis_id is None
        ):
            raise ValueError("execute_sql requires contract and hypothesis identifiers")
        if self.action_type is ActionType.PROFILE:
            if self.contract_id is not None:
                raise ValueError("profile cannot target an answer contract")
            if self.expected_evidence != "profile_context":
                raise ValueError("profile expected_evidence must be profile_context")
        return self


class AnalysisAction(AgentAction):
    action_type: Literal[ActionType.PROFILE, ActionType.EXECUTE_SQL]


class ObservationValidation(_FrozenModel):
    observation_id: Identifier
    contract_id: Identifier
    validation_fingerprint: QueryId
    valid: bool
    error_code: Identifier | None = None
    repairable: bool = False

    @model_validator(mode="after")
    def _validate_result(self) -> ObservationValidation:
        if self.valid and (self.error_code is not None or self.repairable):
            raise ValueError("valid observations cannot carry validation errors")
        if not self.valid and self.error_code is None:
            raise ValueError("invalid observations require an error code")
        return self


class Observation(_FrozenModel):
    observation_id: Identifier
    tool_name: ActionType
    purpose: NonBlankText
    ok: bool
    safe_error: NonBlankText | None = None
    hypothesis_id: Identifier | None = None
    contract_id: Identifier | None = None
    query_id: str | None = None
    columns: tuple[Identifier, ...] = ()
    row_count: NonNegativeInt | None = None
    possibly_truncated: bool = False
    payload: FrozenJsonValue | None = None

    @field_serializer("payload")
    def _serialize_payload(self, value: JsonValue | None) -> object:
        return None if value is None else _thaw_json(value)

    @model_validator(mode="after")
    def _validate_observation(self) -> Observation:
        _require_nonblank(self.purpose)
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("observation columns must be unique")
        if not self.ok:
            if self.safe_error is None or self.payload is not None:
                raise ValueError("failed observations require a safe error and no payload")
            if self.possibly_truncated:
                raise ValueError("failed observations cannot be truncated")
            return self
        if self.safe_error is not None:
            raise ValueError("successful observations cannot carry a safe error")
        if self.tool_name in {ActionType.EXECUTE_SQL, ActionType.PROFILE} and (
            self.query_id is None
            or len(self.query_id) != 64
            or any(character not in "0123456789abcdef" for character in self.query_id)
            or not self.columns
            or self.row_count is None
            or self.payload is None
        ):
            raise ValueError("successful query observations require result metadata and payload")
        if self.tool_name is ActionType.EXECUTE_SQL and (
            self.hypothesis_id is None or self.contract_id is None
        ):
            raise ValueError("execute observations require contract and hypothesis identifiers")
        if self.tool_name is ActionType.PROFILE and self.contract_id is not None:
            raise ValueError("profile observations cannot target an answer contract")
        return self

    @property
    def safe_summary(self) -> FrozenJsonObject:
        summary: dict[str, JsonValue] = {
            "observation_id": self.observation_id,
            "tool_name": self.tool_name.value,
            "purpose": self.purpose,
            "ok": self.ok,
            "possibly_truncated": self.possibly_truncated,
            "columns": self.columns,
        }
        optional: tuple[tuple[str, JsonValue | None], ...] = (
            ("safe_error", self.safe_error),
            ("hypothesis_id", self.hypothesis_id),
            ("contract_id", self.contract_id),
            ("query_id", self.query_id),
            ("row_count", self.row_count),
        )
        summary.update((key, value) for key, value in optional if value is not None)
        return _normalize_and_freeze_object(summary)


class EvidenceItem(_FrozenModel):
    evidence_id: Identifier
    observation_id: Identifier
    hypothesis_id: Identifier
    contract_id: Identifier
    query_id: QueryId
    claim_key: Identifier
    dimensions: tuple[tuple[Identifier, str], ...] = ()
    stance: Literal["supports", "refutes"]
    numeric_value: Decimal | None = None
    unit: NonBlankText | None = None
    verified: bool
    limitations: tuple[NonBlankText, ...] = ()

    @model_validator(mode="after")
    def _validate_evidence(self) -> EvidenceItem:
        dimension_names = tuple(name for name, _ in self.dimensions)
        if len(dimension_names) != len(set(dimension_names)):
            raise ValueError("evidence dimensions must be unique")
        if (self.numeric_value is None) != (self.unit is None):
            raise ValueError("numeric evidence requires both a value and unit")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("evidence limitations must be unique")
        return self


class EvidenceAssessment(_FrozenModel):
    complete: bool
    partial: bool
    premise_not_met: bool
    verified_count: NonNegativeInt
    resolved_hypotheses: tuple[Identifier, ...]
    gaps: tuple[NonBlankText, ...]
    stop_reason: StopReason | None = None

    @model_validator(mode="after")
    def _validate_assessment(self) -> EvidenceAssessment:
        if sum((self.complete, self.partial, self.premise_not_met)) > 1:
            raise ValueError("evidence outcome flags are mutually exclusive")
        if len(self.resolved_hypotheses) != len(set(self.resolved_hypotheses)):
            raise ValueError("resolved hypotheses must be unique")
        if len(self.gaps) != len(set(self.gaps)):
            raise ValueError("evidence gaps must be unique")
        if self.complete and (self.gaps or self.stop_reason is not StopReason.ANSWER_COMPLETE):
            raise ValueError("complete evidence requires answer_complete and no gaps")
        if self.partial and self.stop_reason is not StopReason.EVIDENCE_PARTIAL:
            raise ValueError("partial evidence requires evidence_partial")
        if self.premise_not_met and self.stop_reason is not StopReason.PREMISE_NOT_MET:
            raise ValueError("failed premise requires premise_not_met")
        return self


class RepairDecision(_FrozenModel):
    allowed: bool
    error_code: Identifier | None = None
    stop_reason: StopReason | None = None

    @model_validator(mode="after")
    def _validate_decision(self) -> RepairDecision:
        if self.allowed and self.stop_reason is not None:
            raise ValueError("allowed repair cannot have a stop reason")
        if not self.allowed and self.stop_reason is None:
            raise ValueError("blocked repair requires a stop reason")
        return self


class RepairRecord(_FrozenModel):
    repair_id: Identifier
    kind: Literal["structured_output", "result_contract"]
    target: Identifier
    error_code: Identifier
    outcome: Literal["success", "failed", "blocked"]
    repair_action_type: ActionType | None = None
    repair_purpose: NonBlankText | None = None
    original_observation_id: Identifier | None = None
    repaired_observation_id: Identifier | None = None

    @model_validator(mode="after")
    def _validate_record(self) -> RepairRecord:
        has_action_type = self.repair_action_type is not None
        if has_action_type != (self.repair_purpose is not None):
            raise ValueError("repair action type and purpose must be supplied together")
        if self.kind == "structured_output" and (
            has_action_type
            or self.original_observation_id is not None
            or self.repaired_observation_id is not None
        ):
            raise ValueError("structured-output repairs cannot retain tool details")
        if self.kind == "result_contract":
            if self.original_observation_id is None or not has_action_type:
                raise ValueError(
                    "result-contract repairs require safe observation and action metadata"
                )
            if self.repair_action_type not in {ActionType.PROFILE, ActionType.EXECUTE_SQL}:
                raise ValueError("result-contract repair action must be an analysis action")
            if self.outcome == "success" and self.repaired_observation_id is None:
                raise ValueError("successful result repair requires a repaired observation")
            if self.outcome != "success" and self.repaired_observation_id is not None:
                raise ValueError("unsuccessful repair cannot claim a repaired observation")
        return self


class ModelUsage(_FrozenModel):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt


class StructuredModelRequest(_FrozenModel):
    purpose: ModelPurpose
    system_prompt: NonBlankText
    user_payload: FrozenJsonObjectValue
    output_schema_name: Identifier
    output_schema_summary: FrozenJsonObjectValue
    max_output_tokens: Annotated[int, Field(gt=0)]

    @field_serializer("user_payload", "output_schema_summary")
    def _serialize_json_object(self, value: FrozenJsonObject) -> dict[str, object]:
        return cast(dict[str, object], _thaw_json(value))

    @field_validator("system_prompt")
    @classmethod
    def _validate_system_prompt(cls, value: str) -> str:
        return _require_nonblank(value)

    @model_validator(mode="after")
    def _validate_safe_payload_and_schema(self) -> StructuredModelRequest:
        if _json_has_forbidden_key(self.user_payload, predicate=_is_evaluation_only_key):
            raise ValueError("user payload cannot contain evaluation-only fields")
        if self.output_schema_summary.get("title") != self.output_schema_name:
            raise ValueError("output schema title must match output_schema_name")
        return self

    @classmethod
    def for_output[T: BaseModel](
        cls,
        *,
        purpose: ModelPurpose,
        system_prompt: str,
        user_payload: Mapping[str, object],
        output_type: type[T],
        max_output_tokens: int,
    ) -> StructuredModelRequest:
        return cls(
            purpose=purpose,
            system_prompt=system_prompt,
            user_payload=_normalize_and_freeze_object(user_payload),
            output_schema_name=output_type.__name__,
            output_schema_summary=output_type.model_json_schema(),
            max_output_tokens=max_output_tokens,
        )

    def user_json(self) -> str:
        return json.dumps(
            _thaw_json(self.user_payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def provider_system_prompt(self) -> str:
        """Bind the same JSON contract in both transport and budget accounting."""
        schema = json.dumps(
            _thaw_json(self.output_schema_summary),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            f"{self.system_prompt}\n"
            "Return exactly one JSON object conforming to the following output schema. "
            "Return an instance, not the schema itself; do not use Markdown fences.\n"
            f"Output JSON Schema:\n{schema}"
        )

    def prompt_bytes(self) -> bytes:
        envelope: JsonValue = _normalize_and_freeze_object(
            {
                "messages": (
                    {"role": "system", "content": self.provider_system_prompt()},
                    {"role": "user", "content": self.user_json()},
                ),
                "response_format": {"type": "json_object"},
            }
        )
        serialized_envelope = json.dumps(
            _thaw_json(envelope),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return serialized_envelope + bytes(512)

    def for_repair(
        self,
        *,
        failure_category: AgentModelErrorCategory | str,
    ) -> StructuredModelRequest:
        try:
            safe_category = AgentModelErrorCategory(failure_category)
        except (TypeError, ValueError):
            raise ValueError("unsupported agent model error category") from None
        return StructuredModelRequest(
            purpose="repair",
            system_prompt=(
                "Return exactly one JSON object that conforms to the bound output schema."
            ),
            user_payload={
                "failure_category": safe_category.value,
                "output_schema_name": self.output_schema_name,
                "re_output_instruction": (
                    "Re-output the complete answer as schema-valid JSON only."
                ),
            },
            output_schema_name=self.output_schema_name,
            output_schema_summary=self.output_schema_summary,
            max_output_tokens=self.max_output_tokens,
        )


class StructuredModelResult[T: BaseModel](_FrozenModel):
    output: T
    provider_model: Identifier
    usage: ModelUsage
    latency_ms: NonNegativeInt
    finish_reason: AgentFinishReason | None = None
    output_truncated: bool = False

    @property
    def input_tokens(self) -> int:
        return self.usage.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.usage.output_tokens

    @model_validator(mode="after")
    def _validate_finish(self) -> StructuredModelResult[T]:
        if self.output_truncated != (self.finish_reason is AgentFinishReason.LENGTH):
            raise ValueError("output_truncated must exactly match a length finish reason")
        return self


class ModelReservation(_FrozenModel):
    reservation_id: Identifier
    input_token_upper_bound: NonNegativeInt
    output_token_upper_bound: NonNegativeInt
    reserved_cost_cny: NonNegativeDecimal


class GovernanceSnapshot(_FrozenModel):
    action_loops: NonNegativeInt = 0
    llm_calls: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    execute_calls: NonNegativeInt = 0
    profile_calls: NonNegativeInt = 0
    repair_count: NonNegativeInt = 0
    structured_output_repair_count: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    committed_cost_cny: NonNegativeDecimal = Decimal("0")
    reserved_cost_cny: NonNegativeDecimal = Decimal("0")
    soft_cap_reached: bool = False
    deadline_monotonic: Annotated[float, Field(ge=0)] = 0.0

    @model_validator(mode="after")
    def _validate_counts(self) -> GovernanceSnapshot:
        if self.structured_output_repair_count > self.repair_count:
            raise ValueError("structured repairs cannot exceed total repairs")
        if self.execute_calls + self.profile_calls > self.tool_calls:
            raise ValueError("specialized tool counts cannot exceed total tool calls")
        return self


class FinalAnswer(_FrozenModel):
    status: FinalStatus
    stop_reason: StopReason
    answer: NonBlankText
    evidence_ids: tuple[Identifier, ...] = ()
    completed_dimensions: tuple[Identifier, ...] = ()
    missing_dimensions: tuple[Identifier, ...] = ()
    limitations: tuple[NonBlankText, ...] = ()
    result_summary: FrozenJsonObjectValue | None = None

    @field_serializer("result_summary")
    def _serialize_result_summary(self, value: FrozenJsonObject | None) -> object:
        return None if value is None else _thaw_json(value)

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> FinalAnswer:
        for values in (
            self.evidence_ids,
            self.completed_dimensions,
            self.missing_dimensions,
            self.limitations,
        ):
            if len(values) != len(set(values)):
                raise ValueError("final-answer sequences must be unique")
        if set(self.completed_dimensions) & set(self.missing_dimensions):
            raise ValueError("completed and missing dimensions must be disjoint")
        if self.result_summary is not None and _json_has_forbidden_key(
            self.result_summary,
            predicate=_is_final_summary_forbidden_key,
        ):
            raise ValueError("result summary contains unsafe or evaluation-only fields")
        allowed: dict[FinalStatus, set[StopReason]] = {
            FinalStatus.COMPLETED: {StopReason.ANSWER_COMPLETE, StopReason.PREMISE_NOT_MET},
            FinalStatus.PARTIAL: {
                StopReason.EVIDENCE_PARTIAL,
                StopReason.RESULT_TRUNCATED,
                StopReason.SQL_TIMEOUT,
                StopReason.TASK_TIMEOUT,
            },
            FinalStatus.CLARIFICATION_REQUIRED: {StopReason.MISSING_REQUIRED_FIELDS},
            FinalStatus.REFUSED: {
                StopReason.UNSAFE_REQUEST,
                StopReason.SENSITIVE_DATA_REQUEST,
            },
            FinalStatus.UNSUPPORTED: {
                StopReason.UNSUPPORTED_ANALYSIS,
                StopReason.UNSUPPORTED_DATA_DOMAIN,
            },
            FinalStatus.BUDGET_EXHAUSTED: {
                StopReason.COST_SOFT_CAP,
                StopReason.COST_HARD_CAP,
                StopReason.LLM_CALL_LIMIT,
                StopReason.TOOL_CALL_LIMIT,
                StopReason.EXECUTE_LIMIT,
                StopReason.PROFILE_LIMIT,
                StopReason.ANALYSIS_LOOP_LIMIT,
            },
            FinalStatus.POLICY_BLOCKED: {
                StopReason.SQL_POLICY_REJECTED,
                StopReason.SENSITIVE_RESULT_BLOCKED,
            },
            FinalStatus.MODEL_UNAVAILABLE: {StopReason.MODEL_UNAVAILABLE},
            FinalStatus.EXECUTION_FAILED: {
                StopReason.SQL_TIMEOUT,
                StopReason.TASK_TIMEOUT,
                StopReason.DATABASE_ERROR,
                StopReason.STRUCTURED_OUTPUT_INVALID,
                StopReason.PLAN_INVALID,
                StopReason.ANSWER_CONTRACT_UNMET,
                StopReason.REPAIR_FAILED,
            },
            FinalStatus.INTERNAL_ERROR: {StopReason.INTERNAL_ERROR},
        }
        if self.stop_reason not in allowed[self.status]:
            raise ValueError("stop_reason does not match final status")
        return self


class ModelCallTrace(_FrozenModel):
    purpose: ModelPurpose
    provider_model: Identifier
    outcome: Literal["completed", "failed", "cancelled"]
    safe_error: NonBlankText | None = None
    latency_ms: NonNegativeInt
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    finish_reason: AgentFinishReason | None
    output_truncated: bool
    estimated_cost_cny: NonNegativeDecimal

    @model_validator(mode="after")
    def _validate_trace(self) -> ModelCallTrace:
        if self.outcome == "completed" and self.safe_error is not None:
            raise ValueError("completed model calls cannot carry safe_error")
        if self.outcome != "completed" and self.safe_error is None:
            raise ValueError("failed or cancelled model calls require safe_error")
        if self.output_truncated != (self.finish_reason is AgentFinishReason.LENGTH):
            raise ValueError("output_truncated must exactly match a length finish reason")
        return self


class NodeTrace(_FrozenModel):
    node: Identifier
    duration_ms: NonNegativeInt
    outcome: Literal["completed", "failed", "skipped"]


class ToolCallTrace(_FrozenModel):
    tool_name: ActionType
    purpose: NonBlankText
    safe_arguments: tuple[tuple[Identifier, FrozenJsonValue], ...]
    query_id: str | None = None
    columns: tuple[Identifier, ...] = ()
    row_count: NonNegativeInt | None = None
    possibly_truncated: bool = False
    safe_error: NonBlankText | None = None

    @field_serializer("safe_arguments")
    def _serialize_safe_arguments(
        self, value: tuple[tuple[str, JsonValue], ...]
    ) -> list[list[object]]:
        return [[name, _thaw_json(item)] for name, item in value]

    @model_validator(mode="after")
    def _validate_trace(self) -> ToolCallTrace:
        names = tuple(name for name, _ in self.safe_arguments)
        if len(names) != len(set(names)):
            raise ValueError("safe argument names must be unique")
        self._validate_safe_argument_allowlist()
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("trace columns must be unique")
        if self.query_id is not None and (
            len(self.query_id) != 64
            or any(character not in "0123456789abcdef" for character in self.query_id)
        ):
            raise ValueError("query_id must be a 64-character lowercase hex digest")
        if self.safe_error is not None and (
            self.query_id is not None
            or self.columns
            or self.row_count is not None
            or self.possibly_truncated
        ):
            raise ValueError("failed tool traces cannot claim result metadata")
        return self

    def _validate_safe_argument_allowlist(self) -> None:
        allowed_keys: dict[ActionType, frozenset[str]] = {
            ActionType.METRIC_LOOKUP: frozenset(),
            ActionType.SCHEMA_LOOKUP: frozenset(),
            ActionType.PROFILE: frozenset(
                {
                    "column_name",
                    "filter_columns",
                    "has_time_window",
                    "limit",
                    "operation",
                    "table_name",
                    "time_column",
                }
            ),
            ActionType.EXECUTE_SQL: frozenset({"contract_id", "hypothesis_id"}),
        }
        values = dict(self.safe_arguments)
        if not set(values).issubset(allowed_keys[self.tool_name]):
            raise ValueError("safe arguments contain unknown keys for this tool")
        if self.tool_name in {ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP}:
            return
        if self.tool_name is ActionType.EXECUTE_SQL:
            if any(
                type(value) is not str or re.fullmatch(_IDENTIFIER_PATTERN, value) is None
                for value in values.values()
            ):
                raise ValueError("execute trace arguments must be identifiers")
            return

        identifier_keys = {"table_name", "column_name", "time_column"}
        for name in identifier_keys & values.keys():
            value = values[name]
            if type(value) is not str or re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
                raise ValueError("profile trace identifiers must be bounded")
        if "operation" in values:
            operation = values["operation"]
            if type(operation) is not str or operation not in {
                "time_range",
                "numeric_summary",
                "null_summary",
                "distinct_values",
                "top_values",
            }:
                raise ValueError("profile trace operation is invalid")
        if "has_time_window" in values and type(values["has_time_window"]) is not bool:
            raise ValueError("profile has_time_window must be boolean")
        if "limit" in values and (
            type(values["limit"]) is not int or not 1 <= values["limit"] <= 50
        ):
            raise ValueError("profile limit must be between 1 and 50")
        if "filter_columns" in values:
            filter_columns = values["filter_columns"]
            if not isinstance(filter_columns, tuple) or any(
                type(value) is not str or re.fullmatch(_IDENTIFIER_PATTERN, value) is None
                for value in filter_columns
            ):
                raise ValueError("profile filter_columns must contain identifiers")
            if len(filter_columns) != len(set(filter_columns)):
                raise ValueError("profile filter_columns must be unique")


class SafeTrace(_FrozenModel):
    nodes: tuple[NodeTrace, ...] = ()
    model_calls: tuple[ModelCallTrace, ...] = ()
    tool_calls: tuple[ToolCallTrace, ...] = ()


class ToolInvocation(_FrozenModel):
    observation: Observation
    trace: ToolCallTrace

    @model_validator(mode="after")
    def _validate_pair(self) -> ToolInvocation:
        if (
            self.observation.tool_name is not self.trace.tool_name
            or self.observation.purpose != self.trace.purpose
            or self.observation.query_id != self.trace.query_id
            or self.observation.columns != self.trace.columns
            or self.observation.row_count != self.trace.row_count
            or self.observation.possibly_truncated != self.trace.possibly_truncated
            or self.observation.safe_error != self.trace.safe_error
        ):
            raise ValueError("tool invocation observation and trace metadata must match")
        return self


class StructuredInvocation[T: BaseModel](_FrozenModel):
    result: StructuredModelResult[T]
    traces: tuple[ModelCallTrace, ...]
    repair_record: RepairRecord | None
    governance: GovernanceSnapshot

    @model_validator(mode="after")
    def _validate_invocation(self) -> StructuredInvocation[T]:
        if len(self.traces) not in {1, 2}:
            raise ValueError("structured invocation requires one or two model traces")
        if len(self.traces) == 2 and self.repair_record is None:
            raise ValueError("a repaired invocation requires a repair record")
        return self


class ContextBundle(_FrozenModel):
    ok: bool
    metrics: tuple[MetricInfo, ...]
    tables: tuple[TableInfo, ...]
    observations: tuple[Observation, ...]

    @model_validator(mode="after")
    def _validate_context(self) -> ContextBundle:
        expected_order = (ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP)
        actual_order = tuple(item.tool_name for item in self.observations)
        if self.ok:
            if actual_order != expected_order:
                raise ValueError("successful context requires metric and schema observations")
            if not all(item.ok for item in self.observations):
                raise ValueError("successful context cannot contain failed observations")
        elif (
            not self.observations
            or len(self.observations) > 2
            or actual_order != expected_order[: len(actual_order)]
            or self.observations[-1].ok
            or not all(item.ok for item in self.observations[:-1])
        ):
            raise ValueError("failed context must stop after its first failed lookup")
        return self


class AgentRunResult(_FrozenModel):
    run_id: Identifier
    behavior: BehaviorDecision | None
    answer_contract: AnswerContract | None
    observations: tuple[Observation, ...]
    observation_validations: tuple[ObservationValidation, ...]
    evidence: tuple[EvidenceItem, ...]
    evidence_gaps: tuple[NonBlankText, ...]
    first_candidate: Observation | None
    repair_history: tuple[RepairRecord, ...]
    governance: GovernanceSnapshot
    final_answer: FinalAnswer
    safe_trace: SafeTrace

    @model_validator(mode="after")
    def _validate_history_links(self) -> AgentRunResult:
        observation_ids = tuple(item.observation_id for item in self.observations)
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("observation identifiers must be unique")
        observations_by_id = {item.observation_id: item for item in self.observations}
        validation_ids = tuple(item.observation_id for item in self.observation_validations)
        if len(validation_ids) != len(set(validation_ids)) or not set(validation_ids).issubset(
            observation_ids
        ):
            raise ValueError("validations must uniquely reference observations")
        for validation in self.observation_validations:
            observation = observations_by_id[validation.observation_id]
            if (
                observation.tool_name is not ActionType.EXECUTE_SQL
                or not observation.ok
                or observation.contract_id != validation.contract_id
            ):
                raise ValueError(
                    "validations must target successful execute observations "
                    "with matching contracts"
                )
        validations_by_observation = {
            item.observation_id: item for item in self.observation_validations
        }
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence identifiers must be unique")
        for evidence in self.evidence:
            if not evidence.verified:
                continue
            evidence_observation = observations_by_id.get(evidence.observation_id)
            evidence_validation = validations_by_observation.get(evidence.observation_id)
            if (
                evidence_observation is None
                or evidence_observation.tool_name is not ActionType.EXECUTE_SQL
                or not evidence_observation.ok
                or evidence_validation is None
                or not evidence_validation.valid
                or evidence_observation.contract_id != evidence.contract_id
                or evidence_validation.contract_id != evidence.contract_id
                or evidence_observation.hypothesis_id != evidence.hypothesis_id
                or evidence_observation.query_id != evidence.query_id
            ):
                raise ValueError(
                    "verified evidence must match a valid execute observation and validation"
                )
        verified_evidence_ids = {item.evidence_id for item in self.evidence if item.verified}
        if not set(self.final_answer.evidence_ids).issubset(verified_evidence_ids):
            raise ValueError("final answer can only reference verified evidence")
        if self.first_candidate is not None and self.first_candidate.observation_id not in set(
            observation_ids
        ):
            raise ValueError("first candidate must reference observation history")
        if len(self.evidence_gaps) != len(set(self.evidence_gaps)):
            raise ValueError("evidence gaps must be unique")
        return self


__all__ = [
    "ActionType",
    "AgentAction",
    "AgentFinishReason",
    "AgentModelErrorCategory",
    "AgentRunResult",
    "AnalysisAction",
    "AnalysisType",
    "AnswerContract",
    "BehaviorAction",
    "BehaviorDecision",
    "BehaviorReasonCode",
    "ColumnContract",
    "ContextBundle",
    "EvidenceAssessment",
    "EvidenceItem",
    "FinalAnswer",
    "FinalStatus",
    "FrozenJsonObject",
    "GovernanceSnapshot",
    "Hypothesis",
    "JsonScalar",
    "JsonValue",
    "ModelCallTrace",
    "ModelPurpose",
    "ModelReservation",
    "ModelUsage",
    "NodeTrace",
    "Observation",
    "ObservationContract",
    "ObservationValidation",
    "RepairDecision",
    "RepairRecord",
    "ResultShape",
    "RunLifecycleStatus",
    "SafeTrace",
    "SortKey",
    "StopReason",
    "StructuredInvocation",
    "StructuredModelRequest",
    "StructuredModelResult",
    "TimeWindow",
    "ToolCallTrace",
    "ToolInvocation",
    "TypedMetricPlan",
]
