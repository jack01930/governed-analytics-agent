import pytest

from governed_analytics.safety.sql_policy import ValidatedSql
from governed_analytics.safety.sql_policy import validate_sql as policy_validate_sql
from governed_analytics.tools import (
    ErrorCode,
    ExecuteSqlRequest,
    ExecuteSqlTool,
    MetricRequest,
    MetricTool,
    ProfileFilter,
    ProfileRequest,
    ProfileTool,
    SchemaTool,
)


def test_static_tools_expose_full_ordered_contract() -> None:
    result = SchemaTool().run()
    assert result.ok and result.data is not None
    assert tuple(table.name for table in result.data) == (
        "categories",
        "customers",
        "products",
        "orders",
        "order_items",
        "payments",
        "refunds",
        "inventory_snapshots",
        "web_sessions",
        "marketing_campaigns",
        "campaign_attributions",
        "pipeline_runs",
    )
    metric = MetricTool().run(MetricRequest(metric_id="gmv"))
    assert metric.ok and metric.data is not None


@pytest.mark.asyncio
async def test_execute_rejects_non_readonly_before_database_connection() -> None:
    result = await ExecuteSqlTool().run(ExecuteSqlRequest(sql="drop table orders"))
    assert not result.ok and result.error is not None
    assert result.error.code == ErrorCode.SQL_REJECTED


@pytest.mark.parametrize(
    "sql",
    [
        "select o from orders as o limit 1",
        (
            "select rolname from pg_roles where exists ("
            "with pg_roles as (select category_id from categories) "
            "select 1 from pg_roles)"
        ),
        "select current_role",
        "select 10::regrole",
        "select ctid::text from orders limit 1",
        "select order_id from orders tablesample system (10)",
    ],
)
@pytest.mark.asyncio
async def test_execute_rejects_postgres_metadata_boundaries_before_execution(sql: str) -> None:
    result = await ExecuteSqlTool().run(ExecuteSqlRequest(sql=sql))

    assert not result.ok and result.error is not None
    assert result.error.code is ErrorCode.SQL_REJECTED


@pytest.mark.integration
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
async def test_execute_real_database_rejects_correlated_whole_rows_without_connecting(
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_real_database_enforces_readonly_and_row_cap() -> None:
    result = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql="select order_id from orders order by order_id fetch first 500 rows only"
        )
    )
    assert result.ok, result.error
    assert result.data is not None
    assert result.data.row_limit == 500
    assert result.data.row_count <= 500
    assert result.data.possibly_truncated is True
    assert result.data.row_count == 500


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_real_database_binds_repeated_and_ordered_parameters_safely() -> None:
    result = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql=(
                "select cast(:z as integer) + cast(:z as integer) as repeated_value, "
                "':z and :a' as literal_value, cast(:a as text) as text_value"
            ),
            parameters={"z": 7, "a": "kept"},
        )
    )

    assert result.ok, result.error
    assert result.data is not None
    assert result.data.columns == ("repeated_value", "literal_value", "text_value")
    assert result.data.rows == ((14, ":z and :a", "kept"),)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_real_database_enforces_unqualified_char_one() -> None:
    tool = ExecuteSqlTool()
    accepted = await tool.run(
        ExecuteSqlRequest(
            sql="select cast(:value as char) as value",
            parameters={"value": "x"},
        )
    )
    rejected = await tool.run(
        ExecuteSqlRequest(
            sql="select cast(:value as char) as value",
            parameters={"value": "long"},
        )
    )

    assert accepted.ok, accepted.error
    assert accepted.data is not None
    assert accepted.data.rows == (("x",),)
    assert not rejected.ok and rejected.error is not None
    assert rejected.error.code is ErrorCode.INVALID_REQUEST


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_real_database_preserves_numeric_parameter_values() -> None:
    tool = ExecuteSqlTool()
    sample = await tool.run(
        ExecuteSqlRequest(
            sql="select gross_amount from orders order by order_id fetch first 1 row only"
        )
    )
    assert sample.ok and sample.data is not None
    amount = sample.data.rows[0][0]

    for value in (float(amount), format(amount, "f")):
        result = await tool.run(
            ExecuteSqlRequest(
                sql=(
                    "select count(*) as row_count from orders "
                    "where gross_amount = cast(:amount as numeric(14,2))"
                ),
                parameters={"amount": value},
            )
        )

        assert result.ok, result.error
        assert result.data is not None
        assert result.data.rows[0][0] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_real_database_converts_iso_timestamptz_parameter() -> None:
    tool = ExecuteSqlTool()
    sample = await tool.run(
        ExecuteSqlRequest(
            sql="select order_id, ordered_at from orders order by order_id fetch first 1 row only"
        )
    )
    assert sample.ok and sample.data is not None
    order_id, ordered_at = sample.data.rows[0]

    result = await tool.run(
        ExecuteSqlRequest(
            sql=("select order_id from orders where ordered_at = cast(:ordered_at as timestamptz)"),
            parameters={"ordered_at": ordered_at.isoformat()},
        )
    )

    assert result.ok, result.error
    assert result.data is not None
    assert result.data.rows == ((order_id,),)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_real_database_filters_numeric_and_boolean_columns() -> None:
    sample = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql=(
                "select product_name, category_id, list_price, is_active "
                "from products order by product_id fetch first 1 row only"
            )
        )
    )
    assert sample.ok and sample.data is not None
    product_name, category_id, list_price, is_active = sample.data.rows[0]

    result = await ProfileTool().run(
        ProfileRequest(
            table_name="products",
            column_name="product_name",
            filters=(
                ProfileFilter(column_name="category_id", value=category_id),
                ProfileFilter(column_name="list_price", value=float(list_price)),
                ProfileFilter(column_name="is_active", value=is_active),
            ),
        )
    )

    assert result.ok, result.error
    assert result.data is not None
    assert result.data.rows == ((product_name,),)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_real_database_filters_timestamp_column() -> None:
    sample = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql="select order_id, ordered_at from orders order by order_id fetch first 1 row only"
        )
    )
    assert sample.ok and sample.data is not None
    order_id, ordered_at = sample.data.rows[0]

    result = await ProfileTool().run(
        ProfileRequest(
            table_name="orders",
            column_name="order_id",
            filters=(ProfileFilter(column_name="ordered_at", value=ordered_at.isoformat()),),
        )
    )

    assert result.ok, result.error
    assert result.data is not None
    assert result.data.rows == ((order_id,),)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_real_database_filters_integer_column() -> None:
    sample = await ExecuteSqlTool().run(
        ExecuteSqlRequest(
            sql="select quantity from order_items order by order_item_id fetch first 1 row only"
        )
    )
    assert sample.ok and sample.data is not None
    quantity = sample.data.rows[0][0]

    result = await ProfileTool().run(
        ProfileRequest(
            table_name="order_items",
            column_name="quantity",
            filters=(ProfileFilter(column_name="quantity", value=quantity),),
        )
    )

    assert result.ok, result.error
    assert result.data is not None
    assert result.data.rows == ((quantity,),)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_real_database_applies_all_session_defenses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = ValidatedSql(
        sql=(
            "select current_setting('transaction_read_only') as read_only, "
            "current_setting('transaction_isolation') as isolation, "
            "current_setting('statement_timeout') as timeout, "
            "current_setting('search_path') as search_path, "
            "current_setting('TimeZone') as timezone"
        ),
        query_id="a" * 64,
    )
    monkeypatch.setattr("governed_analytics.tools.tools.validate_sql", lambda _sql: inspection)

    result = await ExecuteSqlTool().run(ExecuteSqlRequest(sql="select 1"))

    assert result.ok, result.error
    assert result.data is not None
    assert result.data.rows == (("on", "repeatable read", "10s", "public, pg_catalog", "UTC"),)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_readonly_role_blocks_write_if_tool_policy_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = ExecuteSqlTool()
    before = await tool.run(ExecuteSqlRequest(sql="select count(*) as row_count from categories"))
    assert before.ok and before.data is not None
    bypass = ValidatedSql(
        sql=(
            "insert into categories (category_code, category_name) "
            "values ('TOOL-BYPASS', 'must not persist') returning category_id"
        ),
        query_id="b" * 64,
    )
    monkeypatch.setattr("governed_analytics.tools.tools.validate_sql", lambda _sql: bypass)

    rejected = await tool.run(ExecuteSqlRequest(sql="select 1"))

    assert not rejected.ok and rejected.error is not None
    assert rejected.error.code is ErrorCode.EXECUTION_FAILED
    monkeypatch.setattr("governed_analytics.tools.tools.validate_sql", policy_validate_sql)
    after = await tool.run(ExecuteSqlRequest(sql="select count(*) as row_count from categories"))
    assert after.ok and after.data is not None
    assert after.data.rows == before.data.rows
