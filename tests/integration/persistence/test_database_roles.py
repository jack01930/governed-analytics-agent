import os

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


def _sync_url(name: str) -> str:
    return os.environ[name].replace("+asyncpg", "").replace("+psycopg", "")


@pytest.mark.integration
def test_readonly_role_can_select_but_cannot_mutate_or_define_schema() -> None:
    with psycopg.connect(_sync_url("DATABASE_URL")) as connection:
        assert connection.execute("select count(*) from categories").fetchone() == (0,)

        with pytest.raises(InsufficientPrivilege):
            connection.execute(
                "insert into categories (category_code, category_name) values ('x', 'x')"
            )
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("update categories set category_name = 'x'")
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("delete from categories")
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("create table forbidden_readonly_table (id bigint)")
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("alter table categories add column forbidden_readonly_column bigint")
        connection.rollback()


@pytest.mark.integration
def test_loader_role_can_mutate_but_cannot_define_schema() -> None:
    with psycopg.connect(_sync_url("LOADER_DATABASE_URL")) as connection:
        inserted = connection.execute(
            """
            insert into pipeline_runs (pipeline_name, started_at, status)
            values ('role-test', now(), 'running')
            returning pipeline_run_id
            """
        ).fetchone()
        assert inserted is not None
        connection.rollback()

        connection.execute("truncate pipeline_runs")
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("create table forbidden_loader_table (id bigint)")
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute(
                "alter table pipeline_runs add column forbidden_loader_column bigint"
            )
        connection.rollback()
