"""Strict, monotonic in-memory event replay for governed Agent runs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from governed_analytics.agent.contracts import JsonScalar, JsonValue

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COLUMN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_QUERY_ID = re.compile(r"^[0-9a-f]{64}$")
_CURSOR = re.compile(r"^([^:]+):(0|[1-9][0-9]*)$")
_MONEY = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_MAX_SEQUENCE = 2**63 - 1
_SENSITIVE_IDENTIFIER = re.compile(
    r"(?:^|[_.:-])(?:api[_-]?key|apikey|secret|password|token|authorization|bearer|"
    r"prompt|payload|endpoint)(?:$|[_.:-])|^(?:sk|pk)-",
    re.IGNORECASE,
)

_BEHAVIOR_ACTIONS = frozenset({"execute", "clarify", "refuse", "unsupported"})
_BEHAVIOR_REASONS = frozenset(
    {
        "ready",
        "missing_metric",
        "missing_time_window",
        "missing_comparison_window",
        "ambiguous_metric",
        "unsafe_request",
        "sensitive_data_request",
        "unsupported_analysis",
        "unsupported_data_domain",
    }
)
_BEHAVIOR_REASON_BY_ACTION: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "execute": frozenset({"ready"}),
        "clarify": frozenset(
            {
                "missing_metric",
                "missing_time_window",
                "missing_comparison_window",
                "ambiguous_metric",
            }
        ),
        "refuse": frozenset({"unsafe_request", "sensitive_data_request"}),
        "unsupported": frozenset({"unsupported_analysis", "unsupported_data_domain"}),
    }
)
_MISSING_FIELDS = frozenset(
    {"metric", "time_window", "previous_window", "current_window", "comparison_window"}
)
_ANALYSIS_TYPES = frozenset({"simple", "comparison", "attribution"})
_HYPOTHESIS_STATUSES = frozenset({"supported", "refuted"})
_ANALYSIS_TOOLS = frozenset({"profile", "execute_sql"})
_SAFE_TOOL_ERRORS = frozenset(
    {
        "malformed_sql",
        "read_only_policy",
        "forbidden_relation",
        "forbidden_function",
        "nondeterministic_query",
        "output_shape_policy",
        "sensitive_output",
        "invalid_request",
        "not_found",
        "sql_timeout",
        "database_error",
        "tool_not_allowed_in_node",
        "internal_tool_error",
    }
)
_VALIDATION_ERRORS = _SAFE_TOOL_ERRORS | frozenset(
    {
        "unknown_tool_error",
        "policy_blocked",
        "sensitive_result_blocked",
        "result_truncated",
        "invalid_query_result_payload",
        "query_result_mismatch",
        "observation_link_mismatch",
        "column_contract_mismatch",
        "row_count_mismatch",
        "row_shape_mismatch",
        "limit_exceeded",
        "nullable_contract_mismatch",
        "type_contract_mismatch",
        "zero_denominator_contract_mismatch",
        "key_not_unique",
        "order_contract_mismatch",
        "answer_contract_unmet",
    }
)
_BUDGET_REASONS = frozenset(
    {
        "cost_soft_cap",
        "cost_hard_cap",
        "llm_call_limit",
        "tool_call_limit",
        "execute_limit",
        "profile_limit",
        "analysis_loop_limit",
        "task_timeout",
        "repair_failed",
    }
)
_FINAL_STATUSES = frozenset(
    {
        "completed",
        "partial",
        "clarification_required",
        "refused",
        "unsupported",
        "budget_exhausted",
        "policy_blocked",
        "model_unavailable",
        "execution_failed",
        "internal_error",
    }
)
_STOP_REASONS = frozenset(
    {
        "answer_complete",
        "premise_not_met",
        "evidence_partial",
        "missing_required_fields",
        "unsupported_analysis",
        "unsupported_data_domain",
        "unsafe_request",
        "sensitive_data_request",
        "sql_policy_rejected",
        "sensitive_result_blocked",
        "cost_soft_cap",
        "cost_hard_cap",
        "llm_call_limit",
        "tool_call_limit",
        "execute_limit",
        "profile_limit",
        "analysis_loop_limit",
        "sql_timeout",
        "task_timeout",
        "result_truncated",
        "database_error",
        "model_unavailable",
        "structured_output_invalid",
        "plan_invalid",
        "answer_contract_unmet",
        "repair_failed",
        "internal_error",
    }
)
_TERMINAL_REASONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "completed": frozenset({"answer_complete", "premise_not_met"}),
        "partial": frozenset(
            {"evidence_partial", "result_truncated", "sql_timeout", "task_timeout"}
        ),
        "clarification_required": frozenset({"missing_required_fields"}),
        "refused": frozenset({"unsafe_request", "sensitive_data_request"}),
        "unsupported": frozenset({"unsupported_analysis", "unsupported_data_domain"}),
        "budget_exhausted": frozenset(
            {
                "cost_soft_cap",
                "cost_hard_cap",
                "llm_call_limit",
                "tool_call_limit",
                "execute_limit",
                "profile_limit",
                "analysis_loop_limit",
            }
        ),
        "policy_blocked": frozenset({"sql_policy_rejected", "sensitive_result_blocked"}),
        "model_unavailable": frozenset({"model_unavailable"}),
        "execution_failed": frozenset(
            {
                "sql_timeout",
                "task_timeout",
                "database_error",
                "structured_output_invalid",
                "plan_invalid",
                "answer_contract_unmet",
                "repair_failed",
            }
        ),
        "internal_error": frozenset({"internal_error"}),
    }
)
_BUDGET_NODES = frozenset(
    {
        "decide_behavior",
        "retrieve_context",
        "build_plan",
        "route_action",
        "invoke_tool",
        "validate_observation",
        "judge_evidence",
        "replan",
        "repair",
        "synthesize",
    }
)
_EVENT_NODES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "run.created": frozenset({"runtime"}),
        "run.started": frozenset({"runtime"}),
        "behavior.decided": frozenset({"decide_behavior"}),
        "context.retrieved": frozenset({"retrieve_context"}),
        "plan.created": frozenset({"build_plan"}),
        "hypothesis.updated": frozenset({"judge_evidence"}),
        "tool.started": frozenset({"invoke_tool", "repair"}),
        "tool.completed": frozenset({"invoke_tool", "repair"}),
        "tool.failed": frozenset({"invoke_tool", "repair"}),
        "observation.validated": frozenset({"validate_observation"}),
        "evidence.assessed": frozenset({"judge_evidence"}),
        "repair.started": frozenset({"repair"}),
        "repair.completed": frozenset({"repair"}),
        "budget.warning": _BUDGET_NODES,
        "run.terminal": frozenset({"runtime"}),
    }
)


class RunNotFound(LookupError):
    def __init__(self) -> None:
        super().__init__("run_not_found")


class InvalidEventCursor(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_event_cursor")


class EventCursorAhead(ValueError):
    def __init__(self) -> None:
        super().__init__("event_cursor_ahead")


class TerminalEventExists(RuntimeError):
    def __init__(self) -> None:
        super().__init__("terminal_event_exists")


class InvalidRunEvent(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_run_event")


def _safe_run_id(value: object) -> str:
    if (
        type(value) is not str
        or _RUN_ID.fullmatch(value) is None
        or _SENSITIVE_IDENTIFIER.search(value) is not None
    ):
        raise InvalidRunEvent()
    return value


def _snapshot_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRunEvent()
    snapshot: dict[str, object] = {}
    try:
        for key, item in value.items():
            if type(key) is not str or key in snapshot:
                raise InvalidRunEvent()
            snapshot[key] = item
    except InvalidRunEvent:
        raise
    except Exception:
        raise InvalidRunEvent() from None
    return snapshot


def _expect_keys(values: Mapping[str, object], expected: frozenset[str]) -> None:
    if frozenset(values) != expected:
        raise InvalidRunEvent()


def _enum(value: object, choices: frozenset[str]) -> str:
    if type(value) is not str or value not in choices:
        raise InvalidRunEvent()
    return value


def _identifier(value: object) -> str:
    if (
        type(value) is not str
        or _IDENTIFIER.fullmatch(value) is None
        or _SENSITIVE_IDENTIFIER.search(value) is not None
    ):
        raise InvalidRunEvent()
    return value


def _optional_identifier(value: object) -> str | None:
    return None if value is None else _identifier(value)


def _nonnegative_int(value: object, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise InvalidRunEvent()
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise InvalidRunEvent()
    return value


def _identifier_tuple(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = True,
) -> tuple[JsonValue, ...]:
    if type(value) is not tuple or len(value) > maximum or (not allow_empty and not value):
        raise InvalidRunEvent()
    items = tuple(_identifier(item) for item in value)
    if len(items) != len(set(items)):
        raise InvalidRunEvent()
    return cast(tuple[JsonValue, ...], items)


def _columns(value: object) -> tuple[JsonValue, ...]:
    if type(value) is not tuple or len(value) > 64:
        raise InvalidRunEvent()
    columns: list[str] = []
    for item in value:
        if (
            type(item) is not str
            or _COLUMN.fullmatch(item) is None
            or _SENSITIVE_IDENTIFIER.search(item) is not None
        ):
            raise InvalidRunEvent()
        columns.append(item)
    if len(columns) != len(set(columns)):
        raise InvalidRunEvent()
    return cast(tuple[JsonValue, ...], tuple(columns))


def _query_id(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _QUERY_ID.fullmatch(value) is None:
        raise InvalidRunEvent()
    return value


def _money(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 64 or _MONEY.fullmatch(value) is None:
        raise InvalidRunEvent()
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise InvalidRunEvent() from None
    if not parsed.is_finite() or parsed < 0:
        raise InvalidRunEvent()
    return value


def _validate_tool_identity(values: Mapping[str, object]) -> tuple[str, str]:
    tool_name = _enum(values["tool_name"], _ANALYSIS_TOOLS)
    purpose = _identifier(values["purpose"])
    if tool_name == "profile" and purpose != "profile_context":
        raise InvalidRunEvent()
    return tool_name, purpose


def _freeze_event_data(node: object, event_type: object, data: object) -> Mapping[str, JsonValue]:
    if type(node) is not str or type(event_type) is not str:
        raise InvalidRunEvent()
    allowed_nodes = _EVENT_NODES.get(event_type)
    if allowed_nodes is None or node not in allowed_nodes:
        raise InvalidRunEvent()
    values = _snapshot_mapping(data)
    frozen: dict[str, JsonValue]
    if event_type == "run.created":
        _expect_keys(values, frozenset({"status"}))
        frozen = {"status": _enum(values["status"], frozenset({"queued"}))}
    elif event_type == "run.started":
        _expect_keys(values, frozenset({"status"}))
        frozen = {"status": _enum(values["status"], frozenset({"running"}))}
    elif event_type == "behavior.decided":
        _expect_keys(values, frozenset({"action", "reason_code", "missing_fields"}))
        missing_fields = _identifier_tuple(values["missing_fields"], maximum=5)
        if any(item not in _MISSING_FIELDS for item in missing_fields):
            raise InvalidRunEvent()
        action = _enum(values["action"], _BEHAVIOR_ACTIONS)
        reason_code = _enum(values["reason_code"], _BEHAVIOR_REASONS)
        if reason_code not in _BEHAVIOR_REASON_BY_ACTION[action] or (
            (action == "clarify") != bool(missing_fields)
        ):
            raise InvalidRunEvent()
        frozen = {
            "action": action,
            "reason_code": reason_code,
            "missing_fields": missing_fields,
        }
    elif event_type == "context.retrieved":
        _expect_keys(values, frozenset({"metric_count", "table_count", "success"}))
        frozen = {
            "metric_count": _nonnegative_int(values["metric_count"], maximum=10_000),
            "table_count": _nonnegative_int(values["table_count"], maximum=10_000),
            "success": _bool(values["success"]),
        }
    elif event_type == "plan.created":
        _expect_keys(
            values,
            frozenset({"plan_id", "revision", "analysis_type", "metric_id", "hypothesis_ids"}),
        )
        revision = _nonnegative_int(values["revision"], maximum=1_000_000)
        if revision == 0:
            raise InvalidRunEvent()
        frozen = {
            "plan_id": _identifier(values["plan_id"]),
            "revision": revision,
            "analysis_type": _enum(values["analysis_type"], _ANALYSIS_TYPES),
            "metric_id": _identifier(values["metric_id"]),
            "hypothesis_ids": _identifier_tuple(
                values["hypothesis_ids"], maximum=16, allow_empty=False
            ),
        }
    elif event_type == "hypothesis.updated":
        _expect_keys(values, frozenset({"hypothesis_id", "status"}))
        frozen = {
            "hypothesis_id": _identifier(values["hypothesis_id"]),
            "status": _enum(values["status"], _HYPOTHESIS_STATUSES),
        }
    elif event_type == "tool.started":
        _expect_keys(values, frozenset({"tool_name", "purpose", "contract_id"}))
        tool_name, purpose = _validate_tool_identity(values)
        contract_id = _optional_identifier(values["contract_id"])
        if (tool_name == "profile") != (contract_id is None):
            raise InvalidRunEvent()
        if contract_id is not None and purpose != contract_id:
            raise InvalidRunEvent()
        frozen = {
            "tool_name": tool_name,
            "purpose": purpose,
            "contract_id": contract_id,
        }
    elif event_type == "tool.completed":
        _expect_keys(
            values,
            frozenset(
                {
                    "tool_name",
                    "purpose",
                    "query_id",
                    "columns",
                    "row_count",
                    "possibly_truncated",
                }
            ),
        )
        tool_name, purpose = _validate_tool_identity(values)
        query_id = _query_id(values["query_id"])
        if query_id is None:
            raise InvalidRunEvent()
        frozen = {
            "tool_name": tool_name,
            "purpose": purpose,
            "query_id": query_id,
            "columns": _columns(values["columns"]),
            "row_count": _nonnegative_int(values["row_count"], maximum=500),
            "possibly_truncated": _bool(values["possibly_truncated"]),
        }
    elif event_type == "tool.failed":
        _expect_keys(values, frozenset({"tool_name", "purpose", "safe_error"}))
        tool_name, purpose = _validate_tool_identity(values)
        frozen = {
            "tool_name": tool_name,
            "purpose": purpose,
            "safe_error": _enum(values["safe_error"], _SAFE_TOOL_ERRORS),
        }
    elif event_type == "observation.validated":
        _expect_keys(values, frozenset({"contract_id", "valid", "error_code", "repairable"}))
        valid = _bool(values["valid"])
        error = values["error_code"]
        if error is not None:
            error = _enum(error, _VALIDATION_ERRORS)
        repairable = _bool(values["repairable"])
        if valid != (error is None) or (valid and repairable):
            raise InvalidRunEvent()
        frozen = {
            "contract_id": _identifier(values["contract_id"]),
            "valid": valid,
            "error_code": cast(JsonScalar, error),
            "repairable": repairable,
        }
    elif event_type == "evidence.assessed":
        _expect_keys(values, frozenset({"verified_count", "gaps", "partial", "complete"}))
        partial = _bool(values["partial"])
        complete = _bool(values["complete"])
        if partial and complete:
            raise InvalidRunEvent()
        frozen = {
            "verified_count": _nonnegative_int(values["verified_count"]),
            "gaps": _identifier_tuple(values["gaps"], maximum=16),
            "partial": partial,
            "complete": complete,
        }
    elif event_type in {"repair.started", "repair.completed"}:
        _expect_keys(values, frozenset({"repair_count", "error_code", "success"}))
        repair_count = _nonnegative_int(values["repair_count"], maximum=1)
        success = _bool(values["success"])
        if repair_count != 1 or (event_type == "repair.started" and success):
            raise InvalidRunEvent()
        frozen = {
            "repair_count": repair_count,
            "error_code": _enum(values["error_code"], _VALIDATION_ERRORS),
            "success": success,
        }
    elif event_type == "budget.warning":
        _expect_keys(
            values,
            frozenset(
                {
                    "reason",
                    "llm_calls",
                    "tool_calls",
                    "execute_calls",
                    "profile_calls",
                    "repair_count",
                    "committed_cost_cny",
                }
            ),
        )
        llm_calls = _nonnegative_int(values["llm_calls"])
        tool_calls = _nonnegative_int(values["tool_calls"])
        execute_calls = _nonnegative_int(values["execute_calls"])
        profile_calls = _nonnegative_int(values["profile_calls"])
        if execute_calls + profile_calls > tool_calls:
            raise InvalidRunEvent()
        frozen = {
            "reason": _enum(values["reason"], _BUDGET_REASONS),
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
            "execute_calls": execute_calls,
            "profile_calls": profile_calls,
            "repair_count": _nonnegative_int(values["repair_count"], maximum=1),
            "committed_cost_cny": _money(values["committed_cost_cny"]),
        }
    else:
        _expect_keys(values, frozenset({"final_status", "stop_reason"}))
        final_status = _enum(values["final_status"], _FINAL_STATUSES)
        stop_reason = _enum(values["stop_reason"], _STOP_REASONS)
        if stop_reason not in _TERMINAL_REASONS[final_status]:
            raise InvalidRunEvent()
        frozen = {
            "final_status": final_status,
            "stop_reason": stop_reason,
        }
    return MappingProxyType(frozen)


def _thaw(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


class EventCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1, max_length=128, pattern=_RUN_ID.pattern)
    sequence: int = Field(ge=0, le=_MAX_SEQUENCE)

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        try:
            return _safe_run_id(value)
        except InvalidRunEvent:
            raise ValueError("run_id must be a safe identifier") from None

    @property
    def event_id(self) -> str:
        return f"{self.run_id}:{self.sequence}"


class RunEvent(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, arbitrary_types_allowed=True
    )

    event_id: str
    sequence: int = Field(ge=1, le=_MAX_SEQUENCE)
    run_id: str = Field(min_length=1, max_length=128, pattern=_RUN_ID.pattern)
    timestamp: datetime
    node: str
    type: str
    data: Mapping[str, JsonValue]

    @model_validator(mode="before")
    @classmethod
    def _validate_wire_event(cls, value: object) -> object:
        values = _snapshot_mapping(value)
        _expect_keys(
            values,
            frozenset({"event_id", "sequence", "run_id", "timestamp", "node", "type", "data"}),
        )
        values["data"] = _freeze_event_data(values["node"], values["type"], values["data"])
        return values

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("timestamp must be UTC aware")
        return value.astimezone(UTC)

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        try:
            return _safe_run_id(value)
        except InvalidRunEvent:
            raise ValueError("run_id must be a safe identifier") from None

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if self.event_id != f"{self.run_id}:{self.sequence}":
            raise ValueError("event_id must match run_id and sequence")
        object.__setattr__(self, "data", _freeze_event_data(self.node, self.type, self.data))
        return self

    @field_serializer("data")
    def _serialize_data(self, value: Mapping[str, JsonValue]) -> dict[str, object]:
        return {key: _thaw(item) for key, item in value.items()}


class EventStore(Protocol):
    async def create_run(self, run_id: str) -> None: ...

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> RunEvent: ...

    async def emit_terminal(self, run_id: str, data: Mapping[str, JsonValue]) -> RunEvent: ...

    async def high_water_mark(self, run_id: str) -> int: ...

    async def has_terminal(self, run_id: str) -> bool: ...

    def stream(self, run_id: str, *, after_sequence: int | None) -> AsyncIterator[RunEvent]: ...

    async def delete_run(self, run_id: str, *, allow_unstarted: bool = False) -> bool: ...


@dataclass(slots=True)
class _RunBuffer:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    events: list[RunEvent] = field(default_factory=list)
    terminal: bool = False
    started: bool = False
    active_streams: int = 0
    deleted: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryEventStore:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._registry_lock = asyncio.Lock()
        self._runs: dict[str, _RunBuffer] = {}

    async def create_run(self, run_id: str) -> None:
        safe_run_id = _safe_run_id(run_id)
        async with self._registry_lock:
            if safe_run_id in self._runs:
                raise InvalidRunEvent()
            self._runs[safe_run_id] = _RunBuffer()

    async def _locked_buffer(self, run_id: str) -> tuple[_RunBuffer, asyncio.Condition]:
        safe_run_id = _safe_run_id(run_id)
        await self._registry_lock.acquire()
        buffer = self._runs.get(safe_run_id)
        if buffer is None:
            self._registry_lock.release()
            raise RunNotFound()
        try:
            await buffer.condition.acquire()
        except BaseException:
            self._registry_lock.release()
            raise
        self._registry_lock.release()
        if buffer.deleted:
            buffer.condition.release()
            raise RunNotFound()
        return buffer, buffer.condition

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> RunEvent:
        if event_type == "run.terminal":
            raise InvalidRunEvent()
        return await self._append(run_id, node, event_type, data, terminal=False)

    async def emit_terminal(
        self,
        run_id: str,
        data: Mapping[str, JsonValue],
    ) -> RunEvent:
        return await self._append(run_id, "runtime", "run.terminal", data, terminal=True)

    async def _append(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
        *,
        terminal: bool,
    ) -> RunEvent:
        safe_run_id = _safe_run_id(run_id)
        owned_data = _freeze_event_data(node, event_type, data)
        buffer, condition = await self._locked_buffer(safe_run_id)
        try:
            if buffer.terminal:
                raise TerminalEventExists()
            sequence = len(buffer.events) + 1
            timestamp = self._clock()
            event = RunEvent(
                event_id=f"{safe_run_id}:{sequence}",
                sequence=sequence,
                run_id=safe_run_id,
                timestamp=timestamp,
                node=node,
                type=event_type,
                data=owned_data,
            )
            buffer.events.append(event)
            if event_type == "run.started":
                buffer.started = True
            if terminal:
                buffer.terminal = True
            condition.notify_all()
            return event
        except (InvalidRunEvent, TerminalEventExists):
            raise
        except Exception:
            raise InvalidRunEvent() from None
        finally:
            condition.release()

    async def high_water_mark(self, run_id: str) -> int:
        buffer, condition = await self._locked_buffer(run_id)
        try:
            return len(buffer.events)
        finally:
            condition.release()

    async def has_terminal(self, run_id: str) -> bool:
        buffer, condition = await self._locked_buffer(run_id)
        try:
            return buffer.terminal
        finally:
            condition.release()

    async def stream(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
    ) -> AsyncGenerator[RunEvent, None]:
        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0):
            raise InvalidEventCursor()
        buffer, condition = await self._locked_buffer(run_id)
        if after_sequence is not None and after_sequence > len(buffer.events):
            condition.release()
            raise EventCursorAhead()
        buffer.active_streams += 1
        condition.release()
        cursor = after_sequence or 0
        try:
            while True:
                async with buffer.condition:
                    if buffer.deleted:
                        raise RunNotFound()
                    batch = tuple(event for event in buffer.events if event.sequence > cursor)
                    if not batch:
                        if buffer.terminal:
                            return
                        await buffer.condition.wait()
                        continue
                for event in batch:
                    if event.sequence <= cursor:
                        continue
                    cursor = event.sequence
                    yield event
                    if event.type == "run.terminal":
                        return
        finally:
            async with buffer.condition:
                buffer.active_streams -= 1
                buffer.condition.notify_all()

    async def delete_run(self, run_id: str, *, allow_unstarted: bool = False) -> bool:
        if type(allow_unstarted) is not bool:
            raise InvalidRunEvent()
        safe_run_id = _safe_run_id(run_id)
        async with self._registry_lock:
            buffer = self._runs.get(safe_run_id)
            if buffer is None:
                raise RunNotFound()
            async with buffer.condition:
                if buffer.active_streams:
                    return False
                permitted = buffer.terminal or (
                    allow_unstarted and not buffer.started and not buffer.terminal
                )
                if not permitted:
                    return False
                buffer.deleted = True
                del self._runs[safe_run_id]
                buffer.events.clear()
                buffer.condition.notify_all()
                return True


@dataclass(frozen=True, slots=True)
class BoundEventSink:
    store: EventStore
    run_id: str

    def __post_init__(self) -> None:
        _safe_run_id(self.run_id)

    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        if event_type.startswith("run.") or node == "runtime":
            raise InvalidRunEvent()
        await self.store.emit(self.run_id, node, event_type, data)


def parse_last_event_id(
    run_id: str,
    value: str | None,
    *,
    high_water_mark: int,
) -> int | None:
    try:
        safe_run_id = _safe_run_id(run_id)
    except InvalidRunEvent:
        raise InvalidEventCursor() from None
    if type(high_water_mark) is not int or high_water_mark < 0:
        raise InvalidEventCursor()
    if value is None:
        return None
    if type(value) is not str:
        raise InvalidEventCursor()
    match = _CURSOR.fullmatch(value)
    if match is None or match.group(1) != safe_run_id:
        raise InvalidEventCursor()
    try:
        sequence = int(match.group(2))
    except ValueError:
        raise InvalidEventCursor() from None
    if sequence > high_water_mark:
        raise EventCursorAhead()
    return sequence


__all__ = [
    "BoundEventSink",
    "EventCursor",
    "EventCursorAhead",
    "EventStore",
    "InMemoryEventStore",
    "InvalidEventCursor",
    "InvalidRunEvent",
    "RunEvent",
    "RunNotFound",
    "TerminalEventExists",
    "parse_last_event_id",
]
