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
                """
                select tablename
                from pg_tables
                where schemaname = 'public' and tablename <> 'alembic_version'
                """
            )
        }
        vector_enabled = connection.execute(
            "select exists(select 1 from pg_extension where extname = 'vector')"
        ).fetchone()

    assert tables == EXPECTED_TABLES
    assert vector_enabled == (True,)


@pytest.mark.integration
def test_every_foreign_key_has_an_ordered_leading_index_key_prefix() -> None:
    """Require every ordered FK tuple as the leading key prefix of a usable full index."""
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    query = """
        select c.conrelid::regclass::text, c.conname, c.conkey
        from pg_constraint c
        where c.contype = 'f'
          and not exists (
            select 1
            from pg_index i
            where i.indrelid = c.conrelid
              and i.indisvalid
              and i.indisready
              and i.indpred is null
              and i.indnkeyatts >= cardinality(c.conkey)
              and (
                select array_agg(index_key.attnum order by index_key.ordinality)
                from unnest(i.indkey::smallint[]) with ordinality
                  as index_key(attnum, ordinality)
                where index_key.ordinality <= cardinality(c.conkey)
              ) = c.conkey
          )
        order by 1, 2, 3
    """
    with psycopg.connect(url) as connection:
        missing = connection.execute(query).fetchall()

    assert missing == []
