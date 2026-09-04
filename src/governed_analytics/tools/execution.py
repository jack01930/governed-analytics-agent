"""Shared read-only SQL execution backends with explicit engine ownership."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from governed_analytics.safety.sql_policy import ValidatedSql
from governed_analytics.tools.contracts import QueryResult


class SqlExecutionBackend(Protocol):
    """Execute policy-validated SQL without owning the surrounding engine."""

    async def execute(
        self,
        validated: ValidatedSql,
        parameters: tuple[object, ...],
    ) -> QueryResult:
        raise NotImplementedError


class AsyncEngineSqlExecutionBackend:
    """Run one query in a hardened transaction on a caller-owned engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def execute(
        self,
        validated: ValidatedSql,
        parameters: tuple[object, ...],
    ) -> QueryResult:
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(
                text("set transaction isolation level repeatable read, read only")
            )
            await connection.execute(text("set local statement_timeout = '10s'"))
            await connection.execute(text("set local search_path = public, pg_catalog"))
            await connection.execute(text("set local time zone 'UTC'"))
            execution = await connection.exec_driver_sql(
                validated.driver_sql or validated.sql,
                parameters,
            )
            rows = tuple(tuple(row) for row in execution.fetchall())
            return QueryResult(
                query_id=validated.query_id,
                columns=tuple(map(str, execution.keys())),
                rows=rows,
                row_count=len(rows),
                possibly_truncated=len(rows) == validated.row_limit,
            )


__all__ = ["AsyncEngineSqlExecutionBackend", "SqlExecutionBackend"]
