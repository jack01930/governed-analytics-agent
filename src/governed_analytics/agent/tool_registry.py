"""Typed Agent tool registry with safe adapters, permissions, and diagnostics."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    JsonScalar,
    JsonValue,
    Observation,
    ToolCallTrace,
    ToolInvocation,
)
from governed_analytics.safety.sql_policy import (
    SqlPolicyError,
    SqlRejectionCode,
    validate_sql,
)
from governed_analytics.tools.contracts import (
    ErrorCode,
    ExecuteSqlRequest,
    MetricInfo,
    ProfileFilter,
    ProfileRequest,
    ProfileResult,
    QueryResult,
    SchemaRequest,
    TableInfo,
    ToolResponse,
)
from governed_analytics.tools.tools import ExecuteSqlTool, MetricTool, ProfileTool, SchemaTool

type ToolRiskLevel = Literal["low", "medium", "high"]


class SafeSqlDiagnostic(StrEnum):
    """Closed, non-sensitive diagnostics safe for traces and later wire projection."""

    MALFORMED_SQL = "malformed_sql"
    READ_ONLY_POLICY = "read_only_policy"
    FORBIDDEN_RELATION = "forbidden_relation"
    FORBIDDEN_FUNCTION = "forbidden_function"
    NONDETERMINISTIC_QUERY = "nondeterministic_query"
    OUTPUT_SHAPE_POLICY = "output_shape_policy"
    SENSITIVE_OUTPUT = "sensitive_output"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    SQL_TIMEOUT = "sql_timeout"
    DATABASE_ERROR = "database_error"
    TOOL_NOT_ALLOWED_IN_NODE = "tool_not_allowed_in_node"


SQL_DIAGNOSTICS: Mapping[SqlRejectionCode, SafeSqlDiagnostic] = {
    SqlRejectionCode.EMPTY_SQL: SafeSqlDiagnostic.MALFORMED_SQL,
    SqlRejectionCode.INVALID_SQL: SafeSqlDiagnostic.MALFORMED_SQL,
    SqlRejectionCode.MULTIPLE_STATEMENTS: SafeSqlDiagnostic.READ_ONLY_POLICY,
    SqlRejectionCode.NOT_READONLY_QUERY: SafeSqlDiagnostic.READ_ONLY_POLICY,
    SqlRejectionCode.FORBIDDEN_STATEMENT: SafeSqlDiagnostic.READ_ONLY_POLICY,
    SqlRejectionCode.FORBIDDEN_RELATION: SafeSqlDiagnostic.FORBIDDEN_RELATION,
    SqlRejectionCode.FORBIDDEN_FUNCTION: SafeSqlDiagnostic.FORBIDDEN_FUNCTION,
    SqlRejectionCode.NONDETERMINISTIC_FUNCTION: SafeSqlDiagnostic.NONDETERMINISTIC_QUERY,
    SqlRejectionCode.SELECT_STAR: SafeSqlDiagnostic.OUTPUT_SHAPE_POLICY,
    SqlRejectionCode.SENSITIVE_RAW_OUTPUT: SafeSqlDiagnostic.SENSITIVE_OUTPUT,
    SqlRejectionCode.WITH_TIES: SafeSqlDiagnostic.OUTPUT_SHAPE_POLICY,
}

_TOOL_DIAGNOSTICS: Mapping[ErrorCode, SafeSqlDiagnostic] = {
    ErrorCode.INVALID_REQUEST: SafeSqlDiagnostic.INVALID_REQUEST,
    ErrorCode.NOT_FOUND: SafeSqlDiagnostic.NOT_FOUND,
    ErrorCode.SQL_REJECTED: SafeSqlDiagnostic.READ_ONLY_POLICY,
    ErrorCode.QUERY_TIMEOUT: SafeSqlDiagnostic.SQL_TIMEOUT,
    ErrorCode.EXECUTION_FAILED: SafeSqlDiagnostic.DATABASE_ERROR,
    ErrorCode.SENSITIVE_RESULT_BLOCKED: SafeSqlDiagnostic.SENSITIVE_OUTPUT,
}


@dataclass(frozen=True, slots=True)
class ToolDefinition[RequestT: BaseModel, ResultT]:
    """One auditable typed tool definition; heterogeneous erasure stays private."""

    name: ActionType
    input_model: type[RequestT]
    risk_level: ToolRiskLevel
    argument_adapter: Callable[[Mapping[str, JsonValue]], RequestT]
    handler: Callable[[RequestT], Awaitable[ToolResponse[ResultT]]]
    result_sanitizer: Callable[[ResultT], JsonValue]
    allowed_nodes: frozenset[str]
    budget_cost: int


class _RegistryDiagnostic(ValueError):
    def __init__(self, diagnostic: SafeSqlDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.value)


def tool_data_to_json(value: object) -> JsonValue:
    """Recursively normalize tool data to the Agent's JSON-only contract."""

    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("tool data numbers must be finite")
        return value
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonScalar, value)
    if isinstance(value, BaseModel):
        return tool_data_to_json(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("tool data mappings require string keys")
            normalized[key] = tool_data_to_json(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return tuple(tool_data_to_json(item) for item in value)
    raise ValueError("tool data contains a non-JSON value")


def _empty_adapter(arguments: Mapping[str, JsonValue]) -> SchemaRequest:
    if arguments:
        raise ValueError("context tools do not accept arguments")
    return SchemaRequest()


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("datetime arguments must use ISO strings")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("datetime arguments must use ISO strings") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime arguments must be timezone-aware")
    return parsed


def _profile_adapter(arguments: Mapping[str, JsonValue]) -> ProfileRequest:
    permitted = {
        "table_name",
        "column_name",
        "operation",
        "filters",
        "time_column",
        "start_at",
        "end_at",
        "limit",
    }
    if not set(arguments).issubset(permitted):
        raise ValueError("profile arguments contain unknown fields")
    raw_filters = arguments.get("filters", ())
    if not isinstance(raw_filters, tuple):
        raise ValueError("profile filters must be a JSON list")
    filters: list[ProfileFilter] = []
    for item in raw_filters:
        if not isinstance(item, Mapping):
            raise ValueError("profile filters must be JSON objects")
        filters.append(ProfileFilter.model_validate(dict(item)))
    values: dict[str, object] = {
        "table_name": arguments.get("table_name"),
        "column_name": arguments.get("column_name"),
        "filters": tuple(filters),
    }
    for name in ("operation", "time_column", "limit"):
        if name in arguments:
            values[name] = arguments[name]
    if "start_at" in arguments:
        values["start_at"] = _parse_datetime(arguments["start_at"])
    if "end_at" in arguments:
        values["end_at"] = _parse_datetime(arguments["end_at"])
    return ProfileRequest.model_validate(values)


def _execute_adapter(arguments: Mapping[str, JsonValue]) -> ExecuteSqlRequest:
    sql = arguments.get("sql")
    if type(sql) is not str:
        raise ValueError("execute arguments require SQL text")
    try:
        validate_sql(sql)
    except SqlPolicyError as error:
        raise _RegistryDiagnostic(SQL_DIAGNOSTICS[error.code]) from None
    except ValueError:
        raise _RegistryDiagnostic(SafeSqlDiagnostic.MALFORMED_SQL) from None
    return ExecuteSqlRequest.model_validate(dict(arguments))


def _safe_purpose(action: AgentAction) -> str:
    if action.action_type is ActionType.EXECUTE_SQL:
        return action.contract_id or action.hypothesis_id or "execute_sql"
    if action.action_type is ActionType.PROFILE:
        return "profile_context"
    return action.action_type.value


def _safe_arguments(
    action: AgentAction,
    request: BaseModel | None,
) -> tuple[tuple[str, JsonValue], ...]:
    if action.action_type is ActionType.EXECUTE_SQL:
        values: dict[str, JsonValue] = {}
        if action.contract_id is not None:
            values["contract_id"] = action.contract_id
        if action.hypothesis_id is not None:
            values["hypothesis_id"] = action.hypothesis_id
        return tuple(sorted(values.items()))
    if action.action_type is not ActionType.PROFILE or not isinstance(request, ProfileRequest):
        return ()
    values = {
        "column_name": request.column_name,
        "filter_columns": tuple(item.column_name for item in request.filters),
        "has_time_window": request.start_at is not None,
        "limit": request.limit,
        "operation": request.operation,
        "table_name": request.table_name,
    }
    if request.time_column is not None:
        values["time_column"] = request.time_column
    return tuple(sorted(values.items()))


def _erased_definition[RequestT: BaseModel, ResultT](
    definition: ToolDefinition[RequestT, ResultT],
) -> ToolDefinition[BaseModel, object]:
    return cast(ToolDefinition[BaseModel, object], cast(object, definition))


class ToolRegistry:
    """The only Agent-facing path to the four governed analytics tools."""

    def __init__(self, definitions: tuple[ToolDefinition[BaseModel, object], ...]) -> None:
        self._definitions = {definition.name: definition for definition in definitions}
        if set(self._definitions) != set(ActionType):
            raise ValueError("registry requires exactly four tool definitions")

    @classmethod
    def default(
        cls,
        schema_tool: SchemaTool,
        metric_tool: MetricTool,
        profile_tool: ProfileTool,
        execute_tool: ExecuteSqlTool,
    ) -> ToolRegistry:
        async def list_metrics(
            _request: SchemaRequest,
        ) -> ToolResponse[tuple[MetricInfo, ...]]:
            return metric_tool.list()

        async def load_schema(
            request: SchemaRequest,
        ) -> ToolResponse[tuple[TableInfo, ...]]:
            return schema_tool.run(request)

        definitions = (
            _erased_definition(
                ToolDefinition(
                    name=ActionType.METRIC_LOOKUP,
                    input_model=SchemaRequest,
                    risk_level="low",
                    argument_adapter=_empty_adapter,
                    handler=list_metrics,
                    result_sanitizer=tool_data_to_json,
                    allowed_nodes=frozenset({"retrieve_context"}),
                    budget_cost=1,
                )
            ),
            _erased_definition(
                ToolDefinition(
                    name=ActionType.SCHEMA_LOOKUP,
                    input_model=SchemaRequest,
                    risk_level="low",
                    argument_adapter=_empty_adapter,
                    handler=load_schema,
                    result_sanitizer=tool_data_to_json,
                    allowed_nodes=frozenset({"retrieve_context"}),
                    budget_cost=1,
                )
            ),
            _erased_definition(
                ToolDefinition(
                    name=ActionType.PROFILE,
                    input_model=ProfileRequest,
                    risk_level="medium",
                    argument_adapter=_profile_adapter,
                    handler=profile_tool.run,
                    result_sanitizer=tool_data_to_json,
                    allowed_nodes=frozenset({"invoke_tool", "repair"}),
                    budget_cost=1,
                )
            ),
            _erased_definition(
                ToolDefinition(
                    name=ActionType.EXECUTE_SQL,
                    input_model=ExecuteSqlRequest,
                    risk_level="high",
                    argument_adapter=_execute_adapter,
                    handler=execute_tool.run,
                    result_sanitizer=tool_data_to_json,
                    allowed_nodes=frozenset({"invoke_tool", "repair"}),
                    budget_cost=1,
                )
            ),
        )
        return cls(definitions)

    async def lookup_metrics(
        self,
        *,
        node: Literal["retrieve_context"],
    ) -> ToolInvocation:
        return await self._invoke_context(ActionType.METRIC_LOOKUP, node=node)

    async def lookup_schema(
        self,
        *,
        node: Literal["retrieve_context"],
    ) -> ToolInvocation:
        return await self._invoke_context(ActionType.SCHEMA_LOOKUP, node=node)

    async def invoke(self, action: AgentAction, *, node: str) -> ToolInvocation:
        definition = self._definitions[action.action_type]
        if action.action_type not in {ActionType.PROFILE, ActionType.EXECUTE_SQL}:
            return self._failure(
                action,
                SafeSqlDiagnostic.TOOL_NOT_ALLOWED_IN_NODE,
                request=None,
            )
        if node not in definition.allowed_nodes:
            return self._failure(
                action,
                SafeSqlDiagnostic.TOOL_NOT_ALLOWED_IN_NODE,
                request=None,
            )
        return await self._invoke_definition(action, definition)

    async def _invoke_context(self, action_type: ActionType, *, node: str) -> ToolInvocation:
        action = AgentAction(
            action_type=action_type,
            purpose=action_type.value,
            arguments={},
            expected_evidence="context",
        )
        definition = self._definitions[action_type]
        if node not in definition.allowed_nodes:
            return self._failure(
                action,
                SafeSqlDiagnostic.TOOL_NOT_ALLOWED_IN_NODE,
                request=None,
            )
        return await self._invoke_definition(action, definition)

    async def _invoke_definition(
        self,
        action: AgentAction,
        definition: ToolDefinition[BaseModel, object],
    ) -> ToolInvocation:
        try:
            request = definition.argument_adapter(action.arguments)
        except _RegistryDiagnostic as error:
            return self._failure(action, error.diagnostic, request=None)
        except (TypeError, ValueError, ValidationError):
            return self._failure(
                action,
                SafeSqlDiagnostic.INVALID_REQUEST,
                request=None,
            )
        try:
            response = await definition.handler(request)
        except Exception:
            return self._failure(
                action,
                SafeSqlDiagnostic.DATABASE_ERROR,
                request=request,
            )
        if not response.ok or response.data is None:
            diagnostic = (
                _TOOL_DIAGNOSTICS.get(response.error.code, SafeSqlDiagnostic.DATABASE_ERROR)
                if response.error is not None
                else SafeSqlDiagnostic.DATABASE_ERROR
            )
            return self._failure(action, diagnostic, request=request)
        try:
            payload = definition.result_sanitizer(response.data)
        except (TypeError, ValueError):
            return self._failure(
                action,
                SafeSqlDiagnostic.DATABASE_ERROR,
                request=request,
            )
        return self._success(action, request=request, result=response.data, payload=payload)

    def _success(
        self,
        action: AgentAction,
        *,
        request: BaseModel,
        result: object,
        payload: JsonValue,
    ) -> ToolInvocation:
        query_id: str | None = None
        columns: tuple[str, ...] = ()
        row_count: int | None = None
        possibly_truncated = False
        if isinstance(result, (QueryResult, ProfileResult)):
            query_id = result.query_id
            columns = result.columns
            row_count = result.row_count
            possibly_truncated = result.possibly_truncated
        purpose = _safe_purpose(action)
        observation = Observation(
            observation_id=uuid4().hex,
            tool_name=action.action_type,
            purpose=purpose,
            ok=True,
            hypothesis_id=action.hypothesis_id,
            contract_id=action.contract_id,
            query_id=query_id,
            columns=columns,
            row_count=row_count,
            possibly_truncated=possibly_truncated,
            payload=payload,
        )
        trace = ToolCallTrace(
            tool_name=action.action_type,
            purpose=purpose,
            safe_arguments=_safe_arguments(action, request),
            query_id=query_id,
            columns=columns,
            row_count=row_count,
            possibly_truncated=possibly_truncated,
        )
        return ToolInvocation(observation=observation, trace=trace)

    def _failure(
        self,
        action: AgentAction,
        diagnostic: SafeSqlDiagnostic,
        *,
        request: BaseModel | None,
    ) -> ToolInvocation:
        purpose = _safe_purpose(action)
        observation = Observation(
            observation_id=uuid4().hex,
            tool_name=action.action_type,
            purpose=purpose,
            ok=False,
            safe_error=diagnostic.value,
            hypothesis_id=action.hypothesis_id,
            contract_id=action.contract_id,
        )
        trace = ToolCallTrace(
            tool_name=action.action_type,
            purpose=purpose,
            safe_arguments=_safe_arguments(action, request),
            safe_error=diagnostic.value,
        )
        return ToolInvocation(observation=observation, trace=trace)


__all__ = [
    "SQL_DIAGNOSTICS",
    "SafeSqlDiagnostic",
    "ToolDefinition",
    "ToolRegistry",
    "tool_data_to_json",
]
