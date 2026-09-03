"""Database-backed contract evidence for Task 1's trusted Oracle SQL."""

from __future__ import annotations

import os
from decimal import Decimal

import psycopg
import pytest

from governed_analytics.evals.golden import load_golden_cases


def _readonly_database_url() -> str:
    return os.environ["DATABASE_URL"].replace("+asyncpg", "")


@pytest.mark.integration
def test_tiny_oracles_execute_readonly_with_expected_shapes_and_anomalies() -> None:
    cases = load_golden_cases("evals/datasets/golden/cases.yaml")
    results: dict[str, tuple[tuple[str, ...], list[tuple[object, ...]]]] = {}
    repeat_counts: tuple[int, int] | None = None

    with (
        psycopg.connect(_readonly_database_url()) as connection,
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("set transaction read only")
        cursor.execute("set local statement_timeout = '10s'")
        cursor.execute("set local search_path = public, pg_catalog")
        assert cursor.execute(
            "select current_setting('transaction_read_only'), "
            "current_setting('statement_timeout'), current_setting('search_path')"
        ).fetchone() == ("on", "10s", "public, pg_catalog")

        for case in cases:
            cursor.execute(case.oracle_sql_path.read_text(encoding="utf-8"))
            description = cursor.description
            assert description is not None
            columns = tuple(column.name for column in description)
            rows = cursor.fetchall()
            results[case.case_id] = (columns, rows)
        cursor.execute(
            """
            with june_active as (
              select distinct o.customer_id
              from orders as o
              where o.status in ('paid', 'completed', 'refunded')
                and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
                and o.ordered_at < timestamptz '2026-07-01T00:00:00Z'
            ), lifetime_counts as (
              select o.customer_id, count(distinct o.order_id) as valid_orders
              from orders as o
              where o.status in ('paid', 'completed', 'refunded')
                and o.ordered_at < timestamptz '2026-07-01T00:00:00Z'
              group by o.customer_id
            )
            select count(*) filter (where lifetime_counts.valid_orders >= 2), count(*)
            from june_active
            left join lifetime_counts using (customer_id)
            """
        )
        repeat_counts = cursor.fetchone()

    assert set(results) == {case.case_id for case in cases}
    assert all(
        len(results[case.case_id][1]) == 1
        for case in cases
        if case.comparison in {"scalar", "boolean"}
    )
    assert Decimal(str(results["G002"][1][0][2])) < 0
    assert tuple(row[0] for row in results["G006"][1]) == (
        "south_conversion",
        "SKU-000001",
        "SKU-000002",
    )
    assert all(Decimal(str(row[3])) < 0 for row in results["G006"][1])
    assert repeat_counts == (196, 198)
    assert results["G015"][1] == [(Decimal("0.98989898989898989899"),)]
    assert len(results["G016"][1]) == 5
    assert results["G017"] == (("is_stale",), [(True,)])
    assert results["G018"][1] == [(Decimal(20),)]
    assert results["G019"][1] == [(10,)]
    assert results["G020"][1] == [(3,)]
