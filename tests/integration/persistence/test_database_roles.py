import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


def _sync_url(name: str) -> str:
    return os.environ[name].replace("+asyncpg", "").replace("+psycopg", "")


@pytest.mark.integration
def test_readonly_role_can_select_but_cannot_mutate_or_define_schema() -> None:
    with psycopg.connect(_sync_url("DATABASE_URL")) as connection:
        category_count = connection.execute("select count(*) from categories").fetchone()
        assert category_count is not None
        assert isinstance(category_count[0], int)

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
            connection.execute("truncate categories")
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("create table forbidden_readonly_table (id bigint)")
        connection.rollback()

        try:
            with pytest.raises(InsufficientPrivilege):
                connection.execute("create temp table forbidden_readonly_temp_table (id bigint)")
        finally:
            connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("alter table categories add column forbidden_readonly_column bigint")
        connection.rollback()


@pytest.mark.integration
def test_loader_role_can_mutate_but_cannot_define_schema() -> None:
    with psycopg.connect(_sync_url("LOADER_DATABASE_URL")) as connection:
        pipeline_name = f"role-test-{uuid4()}"
        inserted = connection.execute(
            """
            insert into pipeline_runs (pipeline_name, started_at, status)
            values (%s, now(), 'running')
            returning pipeline_run_id
            """,
            (pipeline_name,),
        ).fetchone()
        assert inserted is not None
        pipeline_run_id = inserted[0]

        selected = connection.execute(
            "select pipeline_name, status from pipeline_runs where pipeline_run_id = %s",
            (pipeline_run_id,),
        ).fetchone()
        assert selected == (pipeline_name, "running")

        updated = connection.execute(
            """
            update pipeline_runs
            set status = 'succeeded', finished_at = now()
            where pipeline_run_id = %s
            returning status
            """,
            (pipeline_run_id,),
        ).fetchone()
        assert updated == ("succeeded",)

        deleted = connection.execute(
            "delete from pipeline_runs where pipeline_run_id = %s returning pipeline_run_id",
            (pipeline_run_id,),
        ).fetchone()
        assert deleted == (pipeline_run_id,)
        connection.rollback()

        count_before = connection.execute("select count(*) from pipeline_runs").fetchone()
        assert count_before is not None
        connection.execute(
            """
            insert into pipeline_runs (pipeline_name, started_at, status)
            values (%s, now(), 'running')
            """,
            (f"{pipeline_name}-truncate",),
        )
        connection.execute("truncate pipeline_runs")
        assert connection.execute("select count(*) from pipeline_runs").fetchone() == (0,)
        connection.rollback()
        assert connection.execute("select count(*) from pipeline_runs").fetchone() == count_before
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("create table forbidden_loader_table (id bigint)")
        connection.rollback()

        try:
            with pytest.raises(InsufficientPrivilege):
                connection.execute("create temp table forbidden_loader_temp_table (id bigint)")
        finally:
            connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute(
                "alter table pipeline_runs add column forbidden_loader_column bigint"
            )
        connection.rollback()

        with pytest.raises(InsufficientPrivilege):
            connection.execute("select setval('public.categories_category_id_seq', 1, false)")
        connection.rollback()


@pytest.mark.integration
def test_dataset_reset_function_has_only_the_required_execute_grant() -> None:
    admin_url = _sync_url("MIGRATION_DATABASE_URL")
    with psycopg.connect(admin_url) as connection:
        function = connection.execute(
            """
            select p.prosecdef, owner.rolname, p.pronargs, p.proconfig,
              has_function_privilege('analytics_loader', p.oid, 'execute'),
              has_function_privilege('analytics_readonly', p.oid, 'execute'),
              not exists (
                select 1 from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl
                where acl.grantee = 0 and acl.privilege_type = 'EXECUTE'
              ) as public_cannot_execute
            from pg_proc p
            join pg_namespace namespace on namespace.oid = p.pronamespace
            join pg_roles owner on owner.oid = p.proowner
            where namespace.nspname = 'public' and p.proname = 'reset_analytics_dataset'
            """
        ).fetchone()

    assert function == (True, "governed_admin", 0, ["search_path=pg_catalog"], True, False, True)


@pytest.mark.integration
def test_loader_reset_function_rolls_back_rows_and_identity_state() -> None:
    with psycopg.connect(_sync_url("LOADER_DATABASE_URL")) as connection:
        before = connection.execute("select count(*) from public.categories").fetchone()
        assert before is not None
        connection.execute("select public.reset_analytics_dataset()")
        created = connection.execute(
            """
            insert into public.categories (category_code, category_name, created_at)
            values ('rollback-check', 'rollback-check', now())
            returning category_id
            """
        ).fetchone()
        assert created == (1,)
        connection.rollback()
        assert connection.execute("select count(*) from public.categories").fetchone() == before
