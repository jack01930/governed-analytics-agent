from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from governed_analytics.evals import executor
from governed_analytics.evals.sql_guard import SqlRejected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readonly_executor_sets_all_runtime_defenses_and_caps_rows() -> None:
    result = await executor.execute_readonly_sql(
        "select (select setting from pg_settings where name = 'transaction_read_only') "
        "as transaction_read_only, "
        "(select setting from pg_settings where name = 'statement_timeout') as statement_timeout, "
        "(select setting from pg_settings where name = 'search_path') as search_path, "
        "(select setting from pg_settings where name = 'TimeZone') as timezone"
    )

    assert result.columns == (
        "transaction_read_only",
        "statement_timeout",
        "search_path",
        "timezone",
    )
    assert result.rows == (("on", "10000", "public, pg_catalog", "UTC"),)

    capped_rows = await executor.execute_readonly_sql(
        "select order_id from orders"
    )
    assert len(capped_rows.rows) == 500

    fetched_rows = await executor.execute_readonly_sql(
        "select order_id from orders fetch first 10 rows only"
    )
    assert len(fetched_rows.rows) == 10

    with pytest.raises(SqlRejected, match=r"^baseline SQL rejected$"):
        await executor.execute_readonly_sql(
            "select 1 as value from orders order by value fetch first 1 row with ties"
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "select pg_notify('guard-reject-channel', 'message')",
        "select pg_advisory_lock(123456789)",
        "select pg_terminate_backend(pg_backend_pid())",
    ],
)
async def test_executor_never_connects_for_rejected_function_sql(
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    engine_requested = False

    def fail_if_engine_requested(_settings: object) -> None:
        nonlocal engine_requested
        engine_requested = True
        raise AssertionError("rejected function SQL reached the database engine")

    monkeypatch.setattr(executor, "create_async_database_engine", fail_if_engine_requested)

    with pytest.raises(SqlRejected, match=r"^baseline SQL rejected$"):
        await executor.execute_readonly_sql(sql)

    assert not engine_requested


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_rejects_insert_when_guard_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_count = await executor.execute_readonly_sql("select count(*) as count from categories")

    with monkeypatch.context() as patched:
        patched.setattr(
            executor,
            "validate_baseline_sql",
            lambda _sql: (
                "insert into categories (category_code, category_name) values ('guard-bypass', 'x')"
            ),
        )
        with pytest.raises(DBAPIError) as raised:
            await executor.execute_readonly_sql("select 1")

    assert type(raised.value) is DBAPIError
    assert getattr(raised.value.orig, "sqlstate", None) == "25006"
    final_count = await executor.execute_readonly_sql("select count(*) as count from categories")
    assert final_count == original_count


class _FailingConnectionContext:
    async def __aenter__(self) -> Any:
        raise RuntimeError("connection failure")

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> _FailingConnectionContext:
        return _FailingConnectionContext()

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_executor_calls_guard_once_and_disposes_owned_engine_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_calls = 0
    fake_engine = _FakeEngine()

    def fake_guard(sql: str) -> str:
        nonlocal guard_calls
        assert sql == "select 1"
        guard_calls += 1
        return "SELECT 1 LIMIT 500"

    monkeypatch.setattr(executor, "validate_baseline_sql", fake_guard)
    monkeypatch.setattr(executor, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(executor, "create_async_database_engine", lambda _settings: fake_engine)

    with pytest.raises(RuntimeError, match="connection failure"):
        await executor.execute_readonly_sql("select 1")

    assert guard_calls == 1
    assert fake_engine.disposed
