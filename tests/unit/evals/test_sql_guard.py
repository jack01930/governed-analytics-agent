from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from governed_analytics.evals.sql_guard import SqlRejected, validate_baseline_sql


def _outer_limit(sql: str) -> exp.Limit:
    statements = sqlglot.parse(sql, read="postgres")
    assert len(statements) == 1
    assert isinstance(statements[0], exp.Query)
    limit = statements[0].args.get("limit")
    assert isinstance(limit, exp.Limit)
    return limit


def _limit_value(sql: str) -> int:
    expression = _outer_limit(sql).expression
    assert isinstance(expression, exp.Literal)
    assert not expression.is_string
    return int(expression.this)


@pytest.mark.parametrize(
(
    "sql",
    "expected_limit",
),
[
    ("SELECT category_id FROM categories", 500),
    ("select category_id from categories limit 0", 0),
    ("select category_id from categories limit 5", 5),
    ("select category_id from categories limit 500", 500),
    ("select category_id from categories limit 501", 500),
    ("select category_id from categories limit all", 500),
    ("select category_id from categories limit $1", 500),
    ("select category_id from categories limit 10 + 2", 500),
    ("select 1 union all select 2", 500),
],
)
def test_guard_applies_a_safe_outer_limit(sql: str, expected_limit: int) -> None:
    assert _limit_value(validate_baseline_sql(sql)) == expected_limit


def test_guard_preserves_outer_offset_and_inner_top_k_limit() -> None:
    validated = validate_baseline_sql(
        "with top_categories as (select category_id from categories limit 5) "
        "select category_id from top_categories offset 3"
    )

    assert _limit_value(validated) == 500
    assert "OFFSET 3" in validated
    statements = sqlglot.parse(validated, read="postgres")
    assert isinstance(statements[0], exp.Query)
    inner_limit = next(
        node
        for node in statements[0].walk()
        if isinstance(node, exp.Limit)
        and isinstance(node.expression, exp.Literal)
        and node.expression.this == "5"
    )
    assert isinstance(inner_limit.expression, exp.Literal)
    assert inner_limit.expression.this == "5"


def test_guard_output_reparses_as_one_capped_query_without_forbidden_nodes() -> None:
    validated = validate_baseline_sql(
        "with ids as (select category_id from categories limit 5) select * from ids"
    )

    statements = sqlglot.parse(validated, read="postgres")
    assert len(statements) == 1
    assert isinstance(statements[0], exp.Query)
    assert _limit_value(validated) <= 500
    forbidden_types = (exp.Insert, exp.Update, exp.Delete, exp.Into, exp.Lock, exp.Command)
    assert not any(isinstance(node, forbidden_types) for node in statements[0].walk())


def test_guard_accepts_mixed_case_comments_schema_qualification_and_safe_name_literals() -> None:
    validated = validate_baseline_sql(
        "/* pg_sleep and dblink are only words here */ "
        "SeLeCt 'pg_read_file' AS note, public.categories.category_id "
        "FrOm public.categories -- lo_export is also only a comment\n"
        "WHERE category_code = 'set_config'"
    )

    assert _limit_value(validated) == 500


@pytest.mark.parametrize(
    "sql",
    [
        "insert into categories (category_code, category_name) values ('x', 'x')",
        "update categories set category_name = 'x'",
        "delete from categories",
        "create table rejected_guard_test (id bigint)",
        "alter table categories add column rejected_guard_test bigint",
        "drop table categories",
        "grant select on categories to public",
        "revoke select on categories from public",
        "copy categories to stdout",
        "set statement_timeout = '1s'",
        "begin",
        "commit",
        "rollback",
        "select * into rejected_guard_test from categories",
        "select * from categories for update",
        "with changed as (insert into categories (category_code, category_name) "
        "values ('x', 'x') returning category_id) select * from changed",
        "select 1; select 2",
        "\\d categories",
    ],
)
def test_guard_rejects_write_command_and_locking_constructs(sql: str) -> None:
    with pytest.raises(SqlRejected):
        validate_baseline_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select dblink('dbname=postgres', 'select 1')",
        "select LO_EXPORT(1, '/tmp/out')",
        "select lo_import('/tmp/in')",
        "select pg_catalog.pg_ls_dir('/')",
        "select public.pg_read_file('/tmp/secret')",
        "select pg_sleep(1)",
        "select pg_catalog.set_config('search_path', 'public', true)",
        "with nested as (select pg_sleep(1)) select * from nested",
    ],
)
def test_guard_rejects_dangerous_functions_anywhere_case_insensitively(sql: str) -> None:
    with pytest.raises(SqlRejected):
        validate_baseline_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        " \t\n ",
        "```sql\nselect 1\n```",
        "not sql at all",
        "select from",
    ],
)
def test_guard_rejects_invalid_or_markdown_sql_without_echoing_input(sql: str) -> None:
    with pytest.raises(SqlRejected) as raised:
        validate_baseline_sql(sql)

    message = str(raised.value)
    assert message
    assert raised.value.__cause__ is None
    if sql:
        assert sql not in message
    assert "Expected" not in message
    assert "Line" not in message
