"""Materialize trusted golden Oracle queries into versioned JSON results."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import sqlglot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from governed_analytics.config import DatabaseSettings
from governed_analytics.evals.golden import load_golden_cases
from governed_analytics.evals.models import GoldenCase, QueryResult
from governed_analytics.persistence.database import create_async_database_engine

_EXPECTED_CASE_IDS = tuple(f"G{number:03d}" for number in range(1, 21))
_EXPECTED_RUNTIME_SETTINGS = ("on", "repeatable read", "10s", "public, pg_catalog", "UTC")


def serialize_query_result(result: QueryResult) -> str:
    """Return the canonical, version-control-friendly JSON representation."""
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _expected_output_columns(case: GoldenCase) -> tuple[str, ...]:
    statement = sqlglot.parse_one(case.oracle_sql_path.read_text(encoding="utf-8"), read="postgres")
    expressions = getattr(statement, "expressions", ())
    return tuple(str(expression.alias) for expression in expressions)


def _validate_result(case: GoldenCase, result: QueryResult) -> None:
    expected_columns = _expected_output_columns(case)
    if result.columns != expected_columns:
        raise ValueError(f"Oracle result columns do not match {case.case_id}")
    declared_columns = set(case.key_columns) | set(case.numeric_columns)
    if not declared_columns.issubset(result.columns):
        raise ValueError(f"Oracle result omits declared columns for {case.case_id}")

    if case.comparison in {"scalar", "boolean"} and (
        len(result.columns) != 1 or len(result.rows) != 1
    ):
        raise ValueError(f"Oracle result has invalid scalar shape for {case.case_id}")
    if case.comparison == "boolean" and not isinstance(result.rows[0][0], bool):
        raise ValueError(f"Oracle result is not boolean for {case.case_id}")

    if case.key_columns:
        key_indices = tuple(result.columns.index(column) for column in case.key_columns)
        keys = tuple(tuple(row[index] for index in key_indices) for row in result.rows)
        if any(any(value is None for value in key) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError(f"Oracle result has invalid keys for {case.case_id}")
    if case.comparison == "top_k" and not 1 <= len(result.rows) <= 5:
        raise ValueError(f"Oracle result has invalid top-k size for {case.case_id}")

    if case.case_id == "G006" and tuple(row[0] for row in result.rows) != (
        "south_conversion",
        "SKU-000001",
        "SKU-000002",
    ):
        raise ValueError("Oracle result has invalid G006 evidence keys")
    if case.case_id == "G011" and result.columns != ("channel", "conversion_rate"):
        raise ValueError("Oracle result has invalid G011 output columns")
    if case.case_id == "G015" and result.rows[0][0] != Decimal("0.98989898989898989899"):
        raise ValueError("Oracle result has invalid G015 governed truth")
    if case.case_id == "G017" and result.rows[0][0] is not True:
        raise ValueError("Oracle result has invalid G017 freshness truth")
    critical_counts = {"G018": 20, "G019": 10, "G020": 3}
    if case.case_id in critical_counts and result.rows[0][0] != critical_counts[case.case_id]:
        raise ValueError(f"Oracle result has invalid {case.case_id} anomaly truth")


async def _verify_runtime_settings(connection: AsyncConnection) -> tuple[str, str, str, str, str]:
    row = (
        await connection.execute(
            text(
                "select current_setting('transaction_read_only'), "
                "current_setting('transaction_isolation'), "
                "current_setting('statement_timeout'), "
                "current_setting('search_path'), "
                "current_setting('TimeZone')"
            )
        )
    ).one()
    settings = tuple(str(value) for value in row)
    if settings != _EXPECTED_RUNTIME_SETTINGS:
        raise RuntimeError("Oracle transaction settings verification failed")
    return settings


async def _execute_oracle(connection: AsyncConnection, case: GoldenCase) -> QueryResult:
    execution = await connection.execute(text(case.oracle_sql_path.read_text(encoding="utf-8")))
    result = QueryResult(
        columns=tuple(map(str, execution.keys())),
        rows=tuple(tuple(row) for row in execution.fetchall()),
    )
    _validate_result(case, result)
    return result


def _stage_results(results: dict[str, QueryResult], staging_dir: Path) -> None:
    for case_id in _EXPECTED_CASE_IDS:
        (staging_dir / f"{case_id}.json").write_text(
            serialize_query_result(results[case_id]), encoding="utf-8", newline="\n"
        )


def _publish_staged_results(staging_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for case_id in _EXPECTED_CASE_IDS:
        (staging_dir / f"{case_id}.json").replace(output_dir / f"{case_id}.json")


def _cleanup_staging(staging_dir: Path) -> None:
    if not staging_dir.exists():
        return
    for staged_file in staging_dir.iterdir():
        if staged_file.is_file():
            staged_file.unlink()
    staging_dir.rmdir()


async def materialize_oracles(
    cases: Sequence[GoldenCase], output_dir: str | Path
) -> dict[str, QueryResult]:
    """Execute all twenty trusted Oracles in one repeatable-read readonly snapshot."""
    ordered_cases = tuple(cases)
    if tuple(case.case_id for case in ordered_cases) != _EXPECTED_CASE_IDS:
        raise ValueError("Oracle materialization requires ordered G001 through G020 cases")

    destination = Path(output_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = destination.parent / f".{destination.name}-staging-{uuid4().hex}"
    staging_dir.mkdir()
    engine: AsyncEngine | None = None
    try:
        engine = create_async_database_engine(DatabaseSettings())  # type: ignore[call-arg]
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            await connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            await connection.execute(text("SET LOCAL search_path = public, pg_catalog"))
            await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            await _verify_runtime_settings(connection)
            results = {
                case.case_id: await _execute_oracle(connection, case) for case in ordered_cases
            }
        _stage_results(results, staging_dir)
        _publish_staged_results(staging_dir, destination)
        return results
    finally:
        if engine is not None:
            await engine.dispose()
        _cleanup_staging(staging_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize golden Oracle result JSON files.")
    parser.add_argument("--cases", required=True, help="Path to the golden case registry.")
    parser.add_argument("--output", required=True, help="Directory for expected Oracle JSON files.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI boundary for one Oracle materialization run."""
    args = _build_parser().parse_args(argv)
    try:
        results = asyncio.run(
            materialize_oracles(load_golden_cases(args.cases), Path(args.output))
        )
    except Exception:
        print("oracle materialization failed", file=sys.stderr)
        return 1
    print(f"materialized_oracles={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
