import os

import psycopg
import pytest


@pytest.mark.integration
def test_database_is_at_expected_alembic_head() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert revision == ("0002",)
