"""Read-only execution boundary for generated baseline SQL."""

from __future__ import annotations

from sqlalchemy import text

from governed_analytics.config import DatabaseSettings
from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.sql_guard import validate_baseline_sql
from governed_analytics.persistence.database import create_async_database_engine


async def execute_readonly_sql(sql: str) -> QueryResult:
    """Execute one guarded query in a new time-bounded PostgreSQL read-only transaction."""
    validated_sql = validate_baseline_sql(sql)
    settings = DatabaseSettings()  # type: ignore[call-arg]
    engine = create_async_database_engine(settings)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("set transaction read only"))
            await connection.execute(text("set local statement_timeout = '10s'"))
            await connection.execute(text("set local search_path = public, pg_catalog"))
            await connection.execute(text("set local time zone 'UTC'"))
            execution = await connection.execute(text(validated_sql))
            return QueryResult(
                columns=tuple(map(str, execution.keys())),
                rows=tuple(tuple(row) for row in execution.fetchall()),
            )
    finally:
        await engine.dispose()
