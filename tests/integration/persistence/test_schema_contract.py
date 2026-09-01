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
