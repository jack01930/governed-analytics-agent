import os

import psycopg
import pytest

EXPECTED_TABLES = {
    "campaign_attributions",
    "categories",
    "customers",
    "inventory_snapshots",
    "marketing_campaigns",
    "order_items",
    "orders",
    "payments",
    "pipeline_runs",
    "products",
    "refunds",
    "web_sessions",
}


@pytest.mark.integration
def test_schema_contains_all_business_tables_and_vector_extension() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select tablename from pg_tables where schemaname = 'public'"
            )
        }
        vector_enabled = connection.execute(
            "select exists(select 1 from pg_extension where extname = 'vector')"
        ).fetchone()

    assert EXPECTED_TABLES <= tables  # noqa: SIM300
    assert vector_enabled == (True,)


@pytest.mark.integration
def test_every_foreign_key_column_is_indexed() -> None:
    """Require a usable index whose leading column matches every foreign key column."""
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    query = """
        select c.conrelid::regclass::text, a.attname
        from pg_constraint c
        join unnest(c.conkey) as fk_column(attnum) on true
        join pg_attribute a
          on a.attrelid = c.conrelid and a.attnum = fk_column.attnum
        where c.contype = 'f'
          and not exists (
            select 1
            from pg_index i
            where i.indrelid = c.conrelid
              and i.indisvalid
              and i.indisready
              and i.indpred is null
              and i.indnkeyatts > 0
              and i.indkey[0] = fk_column.attnum
          )
        order by 1, 2
    """
    with psycopg.connect(url) as connection:
        missing = connection.execute(query).fetchall()

    assert missing == []
