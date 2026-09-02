from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from governed_analytics.evals import executor


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readonly_executor_sets_all_runtime_defenses_and_caps_rows() -> None:
    result = await executor.execute_readonly_sql(
        "select current_setting('transaction_read_only') as transaction_read_only, "
        "current_setting('statement_timeout') as statement_timeout, "
        "current_setting('search_path') as search_path, "
        "current_setting('TimeZone') as timezone"
    )

    assert result.columns == (
        "transaction_read_only",
        "statement_timeout",
        "search_path",
        "timezone",
    )
    assert result.rows == (("on", "10s", "public, pg_catalog", "UTC"),)

    capped_rows = await executor.execute_readonly_sql(
        "select category_id from categories cross join generate_series(1, 501)"
    )
    assert len(capped_rows.rows) == 500

    fetched_rows = await executor.execute_readonly_sql(
        "select generate_series(1, 1000) as value fetch first 10 rows only"
    )
    assert len(fetched_rows.rows) == 10


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
