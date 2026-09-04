from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import IntEnum
from typing import Any

import pytest
from pydantic import ValidationError

from governed_analytics.safety.sql_policy import ValidatedSql
from governed_analytics.tools import (
    ColumnInfo,
    ErrorCode,
    ExecuteSqlRequest,
    ExecuteSqlResult,
    ExecuteSqlTool,
    MetricRequest,
    MetricTool,
    ProfileFilter,
    ProfileOperation,
    ProfileRequest,
    ProfileTool,
    QueryResult,
    SchemaRequest,
    SchemaTool,
    ToolResponse,
)
from governed_analytics.tools.tools import _profile_filter_value


class _IntegerEnum(IntEnum):
    VALUE = 1


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
        self.disposed = 0
        self.parameters: list[tuple[object, ...]] = []

    async def execute(
        self,
        _validated: ValidatedSql,
        parameters: tuple[object, ...],
    ) -> QueryResult:
        self.calls += 1
        self.parameters.append(parameters)
        if self.error is not None:
            raise self.error
        return self.result


def test_schema_is_static_and_unknown_tables_are_safe() -> None:
    result = SchemaTool().run()
    assert result.ok and result.data is not None and len(result.data) == 12
    unknown = SchemaTool().run(SchemaRequest(table_names=("not_a_table",)))
    assert unknown.error is not None
    assert unknown.error.code == ErrorCode.NOT_FOUND


def test_metric_tool_uses_catalog_and_returns_immutable_contract() -> None:
    result = MetricTool().run(MetricRequest(metric_id="gmv"))
    assert result.ok and result.data is not None
    assert result.data.metric_id == "gmv"
    with pytest.raises(ValidationError):
        result.data.metric_id = "other"


def test_profile_request_is_structured_and_capped() -> None:
    assert ProfileRequest(table_name="orders", column_name="region").limit == 50
    with pytest.raises(ValidationError):
        ProfileRequest(table_name="orders", column_name="region", limit=51)


@pytest.mark.parametrize("value", ["paid", 7, 10.25, True])
def test_profile_filter_accepts_only_strict_bounded_json_scalars(
    value: str | int | float | bool,
) -> None:
    profile_filter = ProfileFilter(column_name="status", value=value)

    assert profile_filter.value == value
    assert type(profile_filter.value) is type(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"nested": "no"},
        [1],
        Decimal("1.25"),
        datetime(2026, 6, 1, tzinfo=UTC),
        float("nan"),
        float("inf"),
        10**18 + 1,
        "x" * 257,
    ],
)
def test_profile_filter_rejects_non_json_or_unbounded_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ProfileFilter(column_name="status", value=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("data_type", "value", "expected"),
    [
        ("integer", -(2**31), -(2**31)),
        ("integer", 2**31 - 1, 2**31 - 1),
        ("bigint", -(2**63), -(2**63)),
        ("bigint", 2**63 - 1, 2**63 - 1),
        ("numeric(14,2)", 999_999_999_999.99, Decimal("999999999999.99")),
        ("numeric(14,2)", 10.25, Decimal("10.25")),
    ],
)
def test_profile_filter_database_boundaries_are_accepted(
    data_type: str, value: int | float, expected: object
) -> None:
    column = ColumnInfo(name="value", data_type=data_type, nullable=False)

    converted = _profile_filter_value(column, value)

    assert converted == expected
    assert type(converted) is type(expected)


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        ("integer", -(2**31) - 1),
        ("integer", 2**31),
        ("bigint", -(2**63) - 1),
        ("bigint", 2**63),
        ("numeric(14,2)", -1_000_000_000_000),
        ("numeric(14,2)", 1_000_000_000_000),
        ("numeric(14,2)", 10.251),
    ],
)
def test_profile_filter_rejects_values_outside_database_type_range(
    data_type: str, value: int | float
) -> None:
    column = ColumnInfo(name="value", data_type=data_type, nullable=False)

    with pytest.raises(ValueError):
        _profile_filter_value(column, value)


@pytest.mark.asyncio
async def test_profile_uses_fixed_query_and_limit() -> None:
    seen: list[str] = []

    async def execute(sql: str, _params: Mapping[str, object]) -> ToolResponse[QueryResult]:
        seen.append(sql)
        return ToolResponse(
            ok=True,
            data=QueryResult(
                query_id="a" * 64,
                columns=("value",),
                rows=(("north",), ("south",)),
                row_count=2,
            ),
        )

    result = await ProfileTool(execute).run(
        ProfileRequest(table_name="orders", column_name="region")
    )
    assert result.ok and result.data is not None
    assert result.data.rows == (("north",), ("south",))
    assert "LIMIT 50" in seen[0]


def test_execute_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ExecuteSqlRequest(sql="select 1", unsafe=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ExecuteSqlRequest(sql="select :value", parameters={"value": {"nested": "no"}})  # type: ignore[dict-item]


def test_execute_request_parameters_are_deeply_immutable_and_bounded() -> None:
    request = ExecuteSqlRequest(
        sql="select :value",
        parameters={"value": 1},
    )

    with pytest.raises(TypeError):
        request.parameters["value"] = 2  # type: ignore[index]
    assert request.model_dump() == {
        "sql": "select :value",
        "parameters": {"value": 1},
    }
    assert request.model_dump_json() == '{"sql":"select :value","parameters":{"value":1}}'

    with pytest.raises(ValidationError):
        ExecuteSqlRequest(
            sql="select :value",
            parameters={"value": 10**18 + 1},
        )
    with pytest.raises(ValidationError):
        ExecuteSqlRequest(
            sql="select :value",
            parameters={"value": 1.0e19},
        )


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1.25"),
        _IntegerEnum.VALUE,
        datetime(2026, 6, 1, tzinfo=UTC),
        {"nested": "no"},
        [1],
        float("nan"),
        float("inf"),
        "x" * 4097,
    ],
)
def test_execute_request_rejects_non_json_or_unbounded_parameter_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ExecuteSqlRequest(
            sql="select cast(:value as text)",
            parameters={"value": value},  # type: ignore[dict-item]
        )


def test_execute_tool_has_no_public_unvalidated_sql_bypass() -> None:
    assert not hasattr(ExecuteSqlTool(), "run_sql")


@pytest.mark.asyncio
async def test_execute_tool_uses_injected_backend_without_disposing_it() -> None:
    backend = SpySqlExecutionBackend(
        result=QueryResult(
            query_id="a" * 64,
            columns=("value",),
            rows=((1,),),
            row_count=1,
        )
    )

    response = await ExecuteSqlTool(backend=backend).run(
        ExecuteSqlRequest(sql="select 1 as value")
    )

    assert response.ok
    assert backend.calls == 1
    assert backend.disposed == 0


def test_profile_rejects_two_execution_injection_paths() -> None:
    async def execute(
        _sql: str, _params: Mapping[str, object]
    ) -> ToolResponse[QueryResult]:
        raise AssertionError("not called")

    with pytest.raises(ValueError, match="choose execute or backend"):
        ProfileTool(execute, backend=SpySqlExecutionBackend())


@pytest.mark.asyncio
async def test_injected_backend_receives_typed_parameters_for_execute_and_profile() -> None:
    backend = SpySqlExecutionBackend()

    execute = await ExecuteSqlTool(backend=backend).run(
        ExecuteSqlRequest(
            sql=(
                "select cast(:amount as numeric(14,2)) as value, "
                "cast(:ordered_at as timestamptz) as ordered_at"
            ),
            parameters={
                "amount": "10.25",
                "ordered_at": "2026-06-01T00:00:00Z",
            },
        )
    )
    profile = await ProfileTool(backend=backend).run(
        ProfileRequest(
            table_name="orders",
            column_name="region",
            filters=(ProfileFilter(column_name="status", value="paid"),),
        )
    )

    assert execute.ok and profile.ok
    assert backend.calls == 2
    assert backend.disposed == 0
    assert backend.parameters[0] == (
        Decimal("10.25"),
        datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert backend.parameters[1] == ("paid",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql_request", "expected_code"),
    [
        (ExecuteSqlRequest(sql="drop table orders"), ErrorCode.SQL_REJECTED),
        (
            ExecuteSqlRequest(
                sql="select cast(:wanted as integer) as value",
                parameters={"other": 1},
            ),
            ErrorCode.SQL_REJECTED,
        ),
        (
            ExecuteSqlRequest(
                sql="select cast(:value as integer) as value",
                parameters={"value": True},
            ),
            ErrorCode.INVALID_REQUEST,
        ),
    ],
)
async def test_shared_and_default_rejections_are_equivalent_and_never_execute(
    monkeypatch: pytest.MonkeyPatch,
    sql_request: ExecuteSqlRequest,
    expected_code: ErrorCode,
) -> None:
    default_calls = 0

    async def execute(_validated: ValidatedSql, _params: tuple[object, ...]) -> QueryResult:
        nonlocal default_calls
        default_calls += 1
        raise AssertionError("rejected input must not execute")

    monkeypatch.setattr("governed_analytics.tools.tools._execute", execute)
    backend = SpySqlExecutionBackend()

    default = await ExecuteSqlTool().run(sql_request)
    shared = await ExecuteSqlTool(backend=backend).run(sql_request)

    assert default.error is not None and shared.error is not None
    assert (default.error.code, default.error.retryable) == (
        shared.error.code,
        shared.error.retryable,
    ) == (expected_code, False)
    assert default_calls == backend.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (TimeoutError("private timeout"), ErrorCode.QUERY_TIMEOUT),
        (RuntimeError("private database error"), ErrorCode.EXECUTION_FAILED),
    ],
)
async def test_shared_and_default_execution_errors_are_equivalent(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_code: ErrorCode,
) -> None:
    default_calls = 0

    async def execute(_validated: ValidatedSql, _params: tuple[object, ...]) -> QueryResult:
        nonlocal default_calls
        default_calls += 1
        raise error

    monkeypatch.setattr("governed_analytics.tools.tools._execute", execute)
    backend = SpySqlExecutionBackend(error=error)
    request = ExecuteSqlRequest(sql="select 1 as value")

    default = await ExecuteSqlTool().run(request)
    shared = await ExecuteSqlTool(backend=backend).run(request)

    assert default.error is not None and shared.error is not None
    assert (default.error.code, default.error.retryable) == (
        shared.error.code,
        shared.error.retryable,
    ) == (expected_code, True)
    assert default_calls == backend.calls == 1


def test_schema_contract_has_keys_types_nullability_enums_and_foreign_keys() -> None:
    result = SchemaTool().run()
    assert result.data is not None
    expected_columns = {
        "categories": ("category_id", "category_code", "category_name", "created_at"),
        "customers": ("customer_id", "customer_code", "segment", "region", "registered_at"),
        "products": (
            "product_id",
            "sku",
            "category_id",
            "product_name",
            "list_price",
            "unit_cost",
            "is_active",
        ),
        "orders": (
            "order_id",
            "order_code",
            "customer_id",
            "status",
            "ordered_at",
            "region",
            "channel",
            "currency",
            "gross_amount",
            "discount_amount",
            "shipping_amount",
            "payable_amount",
            "updated_at",
        ),
        "order_items": (
            "order_item_id",
            "source_line_id",
            "order_id",
            "product_id",
            "quantity",
            "unit_price",
            "discount_amount",
            "gross_amount",
            "net_amount",
        ),
        "payments": (
            "payment_id",
            "payment_code",
            "order_id",
            "status",
            "provider",
            "amount",
            "paid_at",
            "created_at",
        ),
        "refunds": (
            "refund_id",
            "refund_code",
            "order_id",
            "order_item_id",
            "status",
            "amount",
            "reason",
            "refunded_at",
            "created_at",
        ),
        "inventory_snapshots": (
            "inventory_snapshot_id",
            "snapshot_at",
            "product_id",
            "available_qty",
            "reserved_qty",
        ),
        "web_sessions": (
            "session_id",
            "session_code",
            "customer_id",
            "order_id",
            "channel",
            "occurred_at",
            "converted",
            "duration_seconds",
        ),
        "marketing_campaigns": (
            "campaign_id",
            "campaign_code",
            "campaign_name",
            "channel",
            "start_at",
            "end_at",
            "spend",
        ),
        "campaign_attributions": (
            "attribution_id",
            "campaign_id",
            "order_id",
            "attributed_revenue",
            "attributed_at",
        ),
        "pipeline_runs": (
            "pipeline_run_id",
            "pipeline_name",
            "started_at",
            "finished_at",
            "status",
            "watermark",
            "row_count",
            "error_code",
        ),
    }
    assert {
        table.name: tuple(column.name for column in table.columns) for table in result.data
    } == expected_columns
    orders = next(table for table in result.data if table.name == "orders")
    assert [(c.name, c.data_type, c.nullable) for c in orders.columns] == [
        ("order_id", "bigint", False),
        ("order_code", "text", False),
        ("customer_id", "bigint", False),
        ("status", "text", False),
        ("ordered_at", "timestamptz", False),
        ("region", "text", True),
        ("channel", "text", False),
        ("currency", "text", False),
        ("gross_amount", "numeric(14,2)", False),
        ("discount_amount", "numeric(14,2)", False),
        ("shipping_amount", "numeric(14,2)", False),
        ("payable_amount", "numeric(14,2)", False),
        ("updated_at", "timestamptz", False),
    ]
    assert orders.primary_key == ("order_id",)
    assert orders.foreign_keys[0].target_table == "customers"
    assert dict((c.name, c.allowed_values) for c in orders.columns)["status"] == (
        "placed",
        "paid",
        "completed",
        "cancelled",
        "refunded",
    )
    selected = SchemaTool().run(SchemaRequest(table_names=("orders", "categories")))
    assert selected.data is not None
    assert tuple(table.name for table in selected.data) == ("categories", "orders")


def test_response_and_result_invariants_are_fail_closed() -> None:
    with pytest.raises(ValidationError):
        ToolResponse(ok=True)
    with pytest.raises(ValidationError):
        ExecuteSqlResult(query_id="a" * 64, columns=("x",), rows=((1, 2),), row_count=1)
    with pytest.raises(ValidationError):
        ExecuteSqlResult(query_id="bad", columns=("x",), rows=((1,),), row_count=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["time_range", "numeric_summary", "null_summary", "distinct_values", "top_values"]
)
async def test_profile_operations_bind_filters_and_time_window(
    operation: ProfileOperation,
) -> None:
    seen: list[tuple[str, Mapping[str, object]]] = []

    async def execute(sql: str, params: Mapping[str, object]) -> ToolResponse[QueryResult]:
        seen.append((sql, params))
        return ToolResponse(
            ok=True, data=QueryResult(query_id="a" * 64, columns=("x",), rows=((1,),), row_count=1)
        )

    filters = (ProfileFilter(column_name="status", value="paid"),)
    if operation == "time_range":
        request = ProfileRequest(
            table_name="orders",
            column_name="ordered_at",
            operation=operation,
            filters=filters,
            time_column="ordered_at",
            start_at=datetime(2026, 6, 1, tzinfo=UTC),
            end_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
    else:
        request = ProfileRequest(
            table_name="orders",
            column_name="order_id",
            operation=operation,
            filters=filters,
        )
    result = await ProfileTool(execute).run(request)
    assert result.ok
    sql, params = seen[0]
    assert ":filter_0" in sql and "paid" not in sql
    assert "CAST(:filter_0 AS text)" in sql
    assert params["filter_0"] == "paid"
    if operation == "time_range":
        assert ">= CAST(:profile_start_at AS timestamptz)" in sql
        assert "< CAST(:profile_end_at AS timestamptz)" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_name", "target_name", "filter_name", "value", "expected"),
    [
        ("orders", "region", "order_id", 7, 7),
        ("order_items", "product_id", "quantity", 2, 2),
        ("orders", "region", "gross_amount", 10.25, Decimal("10.25")),
        ("products", "category_id", "is_active", True, True),
        ("orders", "order_id", "ordered_at", "2026-06-01T08:30:00+08:00", None),
    ],
)
async def test_profile_filters_are_converted_from_schema_types(
    table_name: str,
    target_name: str,
    filter_name: str,
    value: str | int | float | bool,
    expected: object,
) -> None:
    seen: list[Mapping[str, object]] = []

    async def execute(_sql: str, params: Mapping[str, object]) -> ToolResponse[QueryResult]:
        seen.append(params)
        return ToolResponse(
            ok=True,
            data=QueryResult(query_id="a" * 64, columns=("value",), rows=(), row_count=0),
        )

    result = await ProfileTool(execute).run(
        ProfileRequest(
            table_name=table_name,
            column_name=target_name,
            filters=(ProfileFilter(column_name=filter_name, value=value),),
        )
    )

    assert result.ok
    converted = seen[0]["filter_0"]
    if filter_name == "ordered_at":
        assert isinstance(converted, datetime)
        assert converted.isoformat() == value
    else:
        assert converted == expected
        assert type(converted) is type(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_name", "target_name", "filter_name", "value"),
    [
        ("orders", "region", "order_id", "7"),
        ("orders", "region", "gross_amount", "10.25"),
        ("products", "category_id", "is_active", 1),
        ("orders", "region", "ordered_at", "2026-06-01T08:30:00"),
        ("orders", "region", "ordered_at", True),
        ("orders", "order_id", "region", False),
    ],
)
async def test_profile_filter_type_mismatches_fail_before_execution(
    table_name: str,
    target_name: str,
    filter_name: str,
    value: str | int | float | bool,
) -> None:
    called = False

    async def execute(_sql: str, _params: Mapping[str, object]) -> ToolResponse[QueryResult]:
        nonlocal called
        called = True
        return ToolResponse(
            ok=True,
            data=QueryResult(query_id="a" * 64, columns=("value",), rows=(), row_count=0),
        )

    response = await ProfileTool(execute).run(
        ProfileRequest(
            table_name=table_name,
            column_name=target_name,
            filters=(ProfileFilter(column_name=filter_name, value=value),),
        )
    )

    assert not response.ok and response.error is not None
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_name", "filter_name", "value"),
    [
        ("order_items", "quantity", 2**31),
        ("orders", "gross_amount", 1_000_000_000_000),
        ("orders", "gross_amount", 10.251),
    ],
)
async def test_profile_database_range_mismatches_fail_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    table_name: str,
    filter_name: str,
    value: int | float,
) -> None:
    called = False

    async def fail_execute(*_args: object, **_kwargs: object) -> QueryResult:
        nonlocal called
        called = True
        raise AssertionError("database executor must not be called")

    monkeypatch.setattr("governed_analytics.tools.tools._execute", fail_execute)
    response = await ProfileTool().run(
        ProfileRequest(
            table_name=table_name,
            column_name=filter_name,
            filters=(ProfileFilter(column_name=filter_name, value=value),),
        )
    )

    assert not response.ok and response.error is not None
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert not called


@pytest.mark.asyncio
async def test_sensitive_profile_is_rejected_before_executor_and_type_mismatch_is_safe() -> None:
    called = False

    async def execute(_sql: str, _params: Mapping[str, object]) -> ToolResponse[QueryResult]:
        nonlocal called
        called = True
        return ToolResponse(
            ok=True, data=QueryResult(query_id="a" * 64, columns=("x",), rows=(), row_count=0)
        )

    sensitive = await ProfileTool(execute).run(
        ProfileRequest(table_name="orders", column_name="order_code", operation="distinct_values")
    )
    assert not sensitive.ok and sensitive.error is not None
    assert sensitive.error.code == ErrorCode.SENSITIVE_RESULT_BLOCKED
    assert not called
    sensitive_filter = await ProfileTool(execute).run(
        ProfileRequest(
            table_name="orders",
            column_name="region",
            operation="top_values",
            filters=(ProfileFilter(column_name="order_code", value="ORD-000001"),),
        )
    )
    assert not sensitive_filter.ok and sensitive_filter.error is not None
    assert sensitive_filter.error.code == ErrorCode.SENSITIVE_RESULT_BLOCKED
    assert not called
    bad_type = await ProfileTool(execute).run(
        ProfileRequest(table_name="orders", column_name="status", operation="numeric_summary")
    )
    assert not bad_type.ok and bad_type.error is not None
    assert bad_type.error.code == ErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_execute_parameter_names_are_exact_and_policy_rejection_precedes_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_connect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("database must not be opened")

    monkeypatch.setattr("governed_analytics.tools.tools._execute", fail_connect)
    result = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql="select cast(:wanted as integer)",
            parameters={"other": 1},
        )
    )
    assert not result.ok and result.error is not None
    assert result.error.code == ErrorCode.SQL_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "select order_id, (select o) as leaked from orders as o limit 1",
        "select order_id, (select cast(o as text)) as leaked from orders as o limit 1",
        "select order_id, (select (select o)) as leaked from orders as o limit 1",
        "select system_user",
    ],
)
async def test_execute_rejects_correlated_whole_rows_and_system_user_before_connect(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    connected = False

    def fail_connect(*_args: object, **_kwargs: object) -> object:
        nonlocal connected
        connected = True
        raise AssertionError("database must not be opened")

    monkeypatch.setattr(
        "governed_analytics.tools.tools.create_async_database_engine",
        fail_connect,
    )

    response = await ExecuteSqlTool().run(ExecuteSqlRequest(sql=sql))

    assert not response.ok and response.error is not None
    assert response.error.code is ErrorCode.SQL_REJECTED
    assert not connected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "select :value",
        "select cast(:value as date)",
        "select cast(:value as integer), cast(:value as bigint)",
    ],
)
async def test_execute_rejects_untyped_or_conflicting_parameters_before_connect(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    async def fail_connect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("database must not be opened")

    monkeypatch.setattr("governed_analytics.tools.tools._execute", fail_connect)

    response = await ExecuteSqlTool().run(ExecuteSqlRequest(sql=sql, parameters={"value": 1}))

    assert not response.ok and response.error is not None
    assert response.error.code is ErrorCode.SQL_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cast_type", "value"),
    [
        ("smallint", 2**15),
        ("integer", True),
        ("bigint", "7"),
        ("numeric(14,2)", "01.25"),
        ("numeric(14,2)", 10.251),
        ("numeric(14,2)", 1_000_000_000_000),
        ("boolean", 1),
        ("text", 1),
        ("char", "long"),
        ("varchar(3)", "long"),
        ("timestamptz", "2026-06-01T00:00:00"),
    ],
)
async def test_execute_rejects_parameter_type_and_range_errors_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    cast_type: str,
    value: str | int | float | bool,
) -> None:
    called = False

    async def fail_connect(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("database must not be opened")

    monkeypatch.setattr("governed_analytics.tools.tools._execute", fail_connect)

    response = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql=f"select cast(:value as {cast_type})",
            parameters={"value": value},
        )
    )

    assert not response.ok and response.error is not None
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert not called


@pytest.mark.asyncio
async def test_execute_converts_typed_parameters_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[tuple[str, ...], tuple[str, ...], tuple[object, ...]]] = []

    async def execute(validated: Any, params: tuple[object, ...]) -> QueryResult:
        seen.append((validated.parameter_names, validated.parameter_types, params))
        return QueryResult(
            query_id=validated.query_id,
            columns=("value",),
            rows=((1,),),
            row_count=1,
        )

    monkeypatch.setattr("governed_analytics.tools.tools._execute", execute)

    response = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql=(
                "select cast(:z as numeric(14,2)), cast(:at as timestamptz), "
                "cast(:decimal_text as decimal(14,2)), cast(:empty as text)"
            ),
            parameters={
                "z": 1008.6,
                "at": "2026-06-01T08:30:00+08:00",
                "decimal_text": "1008.60",
                "empty": None,
            },
        )
    )

    assert response.ok
    assert seen == [
        (
            ("at", "decimal_text", "empty", "z"),
            ("timestamptz", "numeric(14,2)", "text", "numeric(14,2)"),
            (
                datetime.fromisoformat("2026-06-01T08:30:00+08:00"),
                Decimal("1008.60"),
                None,
                Decimal("1008.6"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_execute_timeout_is_sanitized_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "private-database-detail"

    async def timeout(*_args: object, **_kwargs: object) -> QueryResult:
        raise TimeoutError(marker)

    monkeypatch.setattr("governed_analytics.tools.tools._execute", timeout)

    response = await ExecuteSqlTool().run(
        ExecuteSqlRequest(sql="select category_id from categories")
    )

    assert not response.ok and response.error is not None
    assert response.error.code is ErrorCode.QUERY_TIMEOUT
    assert response.error.retryable
    assert marker not in response.error.message


@pytest.mark.asyncio
async def test_execute_uses_fixed_readonly_runtime_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://analytics_readonly:test@127.0.0.1:5432/test",
    )
    control_statements: list[str] = []
    driver_calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeExecution:
        def keys(self) -> tuple[str, ...]:
            return ("order_id",)

        def fetchall(self) -> tuple[tuple[int], ...]:
            return ((7,),)

    class FakeConnection:
        @asynccontextmanager
        async def begin(self) -> AsyncIterator[None]:
            yield

        async def execute(
            self,
            statement: Any,
            params: Mapping[str, object] | None = None,
        ) -> FakeExecution:
            assert params is None
            control_statements.append(str(statement))
            return FakeExecution()

        async def exec_driver_sql(
            self,
            statement: str,
            params: tuple[object, ...],
        ) -> FakeExecution:
            driver_calls.append((statement, params))
            return FakeExecution()

    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        @asynccontextmanager
        async def connect(self) -> AsyncIterator[FakeConnection]:
            yield FakeConnection()

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "governed_analytics.tools.tools.create_async_database_engine",
        lambda _settings: engine,
    )

    response = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql=(
                "select order_id from orders where status = cast(:status as text) "
                "and (region = cast(:region as text) or channel = cast(:status as text)) "
                "order by order_id"
            ),
            parameters={"status": "paid", "region": "north"},
        )
    )

    assert response.ok and response.data is not None
    assert response.data.rows == ((7,),)
    assert engine.disposed
    assert control_statements == [
        "set transaction isolation level repeatable read, read only",
        "set local statement_timeout = '10s'",
        "set local search_path = public, pg_catalog",
        "set local time zone 'UTC'",
    ]
    assert driver_calls == [
        (
            "SELECT order_id FROM orders WHERE status = CAST($2 AS TEXT) "
            "AND (region = CAST($1 AS TEXT) OR channel = CAST($2 AS TEXT)) "
            "ORDER BY order_id LIMIT 500",
            ("north", "paid"),
        )
    ]
