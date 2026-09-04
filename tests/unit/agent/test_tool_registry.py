from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from governed_analytics.agent.contracts import ActionType, AgentAction, JsonValue
from governed_analytics.agent.tool_registry import (
    SQL_DIAGNOSTICS,
    SafeSqlDiagnostic,
    ToolDefinition,
    ToolRegistry,
    tool_data_to_json,
)
from governed_analytics.safety.sql_policy import SqlRejectionCode, ValidatedSql, validate_sql
from governed_analytics.tools import (
    ErrorCode,
    ExecuteSqlRequest,
    ExecuteSqlTool,
    MetricInfo,
    MetricTool,
    ProfileRequest,
    ProfileResult,
    ProfileTool,
    QueryResult,
    SchemaRequest,
    SchemaTool,
    TableInfo,
    ToolError,
    ToolResponse,
)


class SpySqlExecutionBackend:
    def __init__(
        self,
        result: QueryResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or QueryResult(
            query_id="a" * 64,
            columns=("value",),
            rows=((1,),),
            row_count=1,
        )
        self.error = error
        self.calls = 0

    async def execute(
        self,
        _validated: ValidatedSql,
        _parameters: tuple[object, ...],
    ) -> QueryResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class RecordingProfileTool(ProfileTool):
    def __init__(self) -> None:
        self.requests: list[ProfileRequest] = []

    async def run(self, request: ProfileRequest) -> ToolResponse[ProfileResult]:
        self.requests.append(request)
        return ToolResponse(
            ok=True,
            data=ProfileResult(
                query_id="b" * 64,
                table_name=request.table_name,
                column_name=request.column_name,
                operation=request.operation,
                columns=("value", "value_count"),
                rows=(("north", 3),),
                row_count=1,
                value_limit=request.limit,
            ),
        )


class CountingMetricTool(MetricTool):
    def __init__(self) -> None:
        self.calls = 0

    def list(self) -> ToolResponse[tuple[MetricInfo, ...]]:
        self.calls += 1
        return super().list()


class CountingSchemaTool(SchemaTool):
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self, request: SchemaRequest | None = None
    ) -> ToolResponse[tuple[TableInfo, ...]]:
        self.calls += 1
        return super().run(request)


def execute_action(
    sql: str,
    *,
    contract_id: str = "metric",
    parameters: Mapping[str, JsonValue] | None = None,
) -> AgentAction:
    arguments: dict[str, JsonValue] = {"sql": sql}
    if parameters is not None:
        arguments["parameters"] = parameters
    return AgentAction(
        action_type=ActionType.EXECUTE_SQL,
        purpose="calculate metric",
        arguments=arguments,
        hypothesis_id="metric_value",
        contract_id=contract_id,
        expected_evidence="metric value",
    )


def profile_action() -> AgentAction:
    return AgentAction.model_validate(
        {
            "action_type": "profile",
            "purpose": "profile order region",
            "arguments": {
                "table_name": "orders",
                "column_name": "region",
                "operation": "top_values",
                "filters": [{"column_name": "status", "value": "paid"}],
                "time_column": "ordered_at",
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-07-01T00:00:00Z",
                "limit": 5,
            },
            "hypothesis_id": "region_contribution",
            "contract_id": None,
            "expected_evidence": "profile_context",
        }
    )


def default_registry(*, backend: SpySqlExecutionBackend | None = None) -> ToolRegistry:
    effective_backend = backend or SpySqlExecutionBackend()
    return ToolRegistry.default(
        SchemaTool(),
        MetricTool(),
        ProfileTool(backend=effective_backend),
        ExecuteSqlTool(backend=effective_backend),
    )


@pytest.mark.asyncio
async def test_registry_adapts_json_lists_and_iso_datetimes() -> None:
    profile_tool = RecordingProfileTool()
    registry = ToolRegistry.default(
        SchemaTool(), MetricTool(), profile_tool, ExecuteSqlTool(backend=SpySqlExecutionBackend())
    )

    invocation = await registry.invoke(profile_action(), node="invoke_tool")

    assert invocation.observation.ok
    assert invocation.trace.tool_name is ActionType.PROFILE
    request = profile_tool.requests[0]
    assert isinstance(request.filters, tuple)
    assert request.start_at == datetime(2026, 6, 1, tzinfo=UTC)
    assert request.end_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert invocation.trace.safe_arguments == (
        ("column_name", "region"),
        ("filter_columns", ("status",)),
        ("has_time_window", True),
        ("limit", 5),
        ("operation", "top_values"),
        ("table_name", "orders"),
        ("time_column", "ordered_at"),
    )


@pytest.mark.asyncio
async def test_registry_policy_rejection_never_reaches_backend() -> None:
    backend = SpySqlExecutionBackend()
    registry = default_registry(backend=backend)

    invocation = await registry.invoke(
        execute_action("drop table orders"), node="invoke_tool"
    )

    assert not invocation.observation.ok
    assert invocation.observation.safe_error == "read_only_policy"
    assert invocation.trace.safe_error == "read_only_policy"
    assert backend.calls == 0


def test_registry_definitions_and_sql_diagnostics_are_closed_and_auditable() -> None:
    assert tuple(field.name for field in fields(ToolDefinition)) == (
        "name",
        "input_model",
        "risk_level",
        "argument_adapter",
        "handler",
        "result_sanitizer",
        "allowed_nodes",
        "budget_cost",
    )
    assert SQL_DIAGNOSTICS == {
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


@pytest.mark.asyncio
async def test_execute_policy_is_checked_again_inside_execute_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_checks = 0
    tool_checks = 0

    def count_registry(sql: str) -> ValidatedSql:
        nonlocal registry_checks
        registry_checks += 1
        return validate_sql(sql)

    def count_tool(sql: str) -> ValidatedSql:
        nonlocal tool_checks
        tool_checks += 1
        return validate_sql(sql)

    monkeypatch.setattr(
        "governed_analytics.agent.tool_registry.validate_sql", count_registry
    )
    monkeypatch.setattr("governed_analytics.tools.tools.validate_sql", count_tool)

    invocation = await default_registry().invoke(
        execute_action("select 1 as value"), node="invoke_tool"
    )

    assert invocation.observation.ok
    assert (registry_checks, tool_checks) == (1, 1)


@pytest.mark.asyncio
async def test_invoke_rejects_context_actions_without_calling_context_handlers() -> None:
    metric_tool = CountingMetricTool()
    schema_tool = CountingSchemaTool()
    registry = ToolRegistry.default(
        schema_tool,
        metric_tool,
        RecordingProfileTool(),
        ExecuteSqlTool(backend=SpySqlExecutionBackend()),
    )
    metric_action = AgentAction(
        action_type=ActionType.METRIC_LOOKUP,
        purpose="model attempted metric lookup",
        arguments={},
        expected_evidence="context",
    )
    schema_action = metric_action.model_copy(update={"action_type": ActionType.SCHEMA_LOOKUP})

    metric = await registry.invoke(metric_action, node="invoke_tool")
    schema = await registry.invoke(schema_action, node="invoke_tool")

    assert metric.observation.safe_error == "tool_not_allowed_in_node"
    assert schema.observation.safe_error == "tool_not_allowed_in_node"
    assert metric_tool.calls == schema_tool.calls == 0


@pytest.mark.asyncio
async def test_dedicated_context_lookups_convert_tuple_results_without_hidden_budgeting() -> None:
    metric_tool = CountingMetricTool()
    schema_tool = CountingSchemaTool()
    registry = ToolRegistry.default(
        schema_tool,
        metric_tool,
        RecordingProfileTool(),
        ExecuteSqlTool(backend=SpySqlExecutionBackend()),
    )

    metric = await registry.lookup_metrics(node="retrieve_context")
    schema = await registry.lookup_schema(node="retrieve_context")

    assert metric.observation.ok and schema.observation.ok
    assert metric.observation.tool_name is ActionType.METRIC_LOOKUP
    assert schema.observation.tool_name is ActionType.SCHEMA_LOOKUP
    assert metric_tool.calls == schema_tool.calls == 1
    assert isinstance(metric.observation.payload, tuple)
    assert isinstance(schema.observation.payload, tuple)
    assert metric.trace.safe_arguments == schema.trace.safe_arguments == ()


@pytest.mark.asyncio
async def test_invalid_adapter_input_is_safe_and_handler_is_not_called() -> None:
    profile_tool = RecordingProfileTool()
    registry = ToolRegistry.default(
        SchemaTool(), MetricTool(), profile_tool, ExecuteSqlTool(backend=SpySqlExecutionBackend())
    )
    invalid = profile_action().model_copy(
        update={"arguments": {"table_name": "orders", "column_name": "region", "limit": 51}}
    )

    invocation = await registry.invoke(invalid, node="invoke_tool")

    assert invocation.observation.safe_error == "invalid_request"
    assert profile_tool.requests == []


@pytest.mark.asyncio
async def test_observation_may_keep_rows_but_safe_trace_never_keeps_sql_or_parameters() -> None:
    sql_sentinel = "select 1 as private_sql_sentinel"
    parameter_sentinel = "private_parameter_sentinel"
    backend = SpySqlExecutionBackend(
        result=QueryResult(
            query_id="c" * 64,
            columns=("private_sql_sentinel",),
            rows=((parameter_sentinel,),),
            row_count=1,
        )
    )
    registry = default_registry(backend=backend)

    invocation = await registry.invoke(
        execute_action(sql_sentinel, parameters={}), node="invoke_tool"
    )

    assert invocation.observation.ok
    assert parameter_sentinel in json.dumps(invocation.observation.model_dump())
    safe = json.dumps(
        {
            "summary": dict(invocation.observation.safe_summary),
            "trace": invocation.trace.model_dump(),
        }
    )
    assert sql_sentinel not in safe
    assert parameter_sentinel not in safe
    assert "rows" not in safe
    assert "parameters" not in safe


@pytest.mark.asyncio
async def test_database_exception_is_reduced_to_stable_metadata() -> None:
    raw_error = "private database exception sentinel"
    registry = default_registry(backend=SpySqlExecutionBackend(error=RuntimeError(raw_error)))

    invocation = await registry.invoke(
        execute_action("select 1 as value"), node="invoke_tool"
    )

    rendered = json.dumps(invocation.model_dump())
    assert invocation.observation.safe_error == "database_error"
    assert raw_error not in rendered


def test_tool_data_to_json_recursively_normalizes_models_sequences_and_mappings() -> None:
    converted = tool_data_to_json(
        {
            "request": ProfileRequest(
                table_name="orders",
                column_name="region",
                time_column="ordered_at",
                start_at=datetime(2026, 6, 1, tzinfo=UTC),
                end_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
            "items": [MetricTool().list().data],
        }
    )

    assert isinstance(converted, Mapping)
    request = converted["request"]
    assert isinstance(request, Mapping)
    assert request["start_at"] == "2026-06-01T00:00:00Z"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_tool_data_to_json_rejects_nonfinite_json_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        tool_data_to_json(value)


@pytest.mark.asyncio
async def test_tool_error_codes_map_to_stable_registry_diagnostics() -> None:
    class FailingProfileTool(ProfileTool):
        async def run(self, _request: ProfileRequest) -> ToolResponse[ProfileResult]:
            return ToolResponse(
                ok=False,
                error=ToolError(
                    code=ErrorCode.SENSITIVE_RESULT_BLOCKED,
                    message="raw provider and database details",
                ),
            )

    registry = ToolRegistry.default(
        SchemaTool(),
        MetricTool(),
        FailingProfileTool(),
        ExecuteSqlTool(backend=SpySqlExecutionBackend()),
    )

    invocation = await registry.invoke(profile_action(), node="invoke_tool")

    assert invocation.observation.safe_error == "sensitive_output"
    assert "raw provider" not in json.dumps(invocation.model_dump())


def test_execute_request_remains_the_strict_public_input_contract() -> None:
    request = ExecuteSqlRequest.model_validate(
        {"sql": "select cast(:value as integer) as value", "parameters": {"value": 7}}
    )

    assert request.parameters == {"value": 7}
