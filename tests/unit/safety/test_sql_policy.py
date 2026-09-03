from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from governed_analytics.safety.catalog import sensitive_raw_columns
from governed_analytics.safety.sql_policy import (
    MAX_RESULT_ROWS,
    SqlPolicyError,
    SqlRejectionCode,
    validate_sql,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ALLOWED_FIXTURES = _REPOSITORY_ROOT / "tests" / "fixtures" / "sql" / "allowed"
_REJECTED_FIXTURES = _REPOSITORY_ROOT / "tests" / "fixtures" / "sql" / "rejected"
_GOLDEN_SQL = _REPOSITORY_ROOT / "evals" / "datasets" / "golden" / "sql"
_EXPECTED_REJECTIONS = {
    "001_multi_statement.sql": SqlRejectionCode.MULTIPLE_STATEMENTS,
    "002_insert.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "003_update.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "004_delete.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "005_create.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "006_alter.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "007_drop.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "008_write_cte.sql": SqlRejectionCode.FORBIDDEN_STATEMENT,
    "009_lock.sql": SqlRejectionCode.FORBIDDEN_STATEMENT,
    "010_copy.sql": SqlRejectionCode.NOT_READONLY_QUERY,
    "011_system_relation.sql": SqlRejectionCode.FORBIDDEN_RELATION,
    "012_unknown_relation.sql": SqlRejectionCode.FORBIDDEN_RELATION,
    "013_dangerous_function.sql": SqlRejectionCode.FORBIDDEN_FUNCTION,
    "014_configuration_function.sql": SqlRejectionCode.FORBIDDEN_FUNCTION,
    "015_current_date.sql": SqlRejectionCode.NONDETERMINISTIC_FUNCTION,
    "016_now.sql": SqlRejectionCode.NONDETERMINISTIC_FUNCTION,
    "017_random.sql": SqlRejectionCode.NONDETERMINISTIC_FUNCTION,
    "018_select_star.sql": SqlRejectionCode.SELECT_STAR,
    "019_sensitive_output.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "020_sensitive_alias.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "021_with_ties.sql": SqlRejectionCode.WITH_TIES,
    "022_sensitive_cte_alias.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "023_sensitive_max.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "024_sensitive_subquery.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "025_sensitive_cast.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "026_sensitive_concat.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "027_sensitive_case.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "028_sensitive_union.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "029_sensitive_intersect.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "030_sensitive_except.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "031_table_function.sql": SqlRejectionCode.FORBIDDEN_RELATION,
    "032_values_dangerous_function.sql": SqlRejectionCode.FORBIDDEN_FUNCTION,
    "033_values_nondeterministic.sql": SqlRejectionCode.NONDETERMINISTIC_FUNCTION,
    "034_pg_catalog_quoted.sql": SqlRejectionCode.FORBIDDEN_RELATION,
    "035_sensitive_quoted_case.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "036_trailing_statement.sql": SqlRejectionCode.MULTIPLE_STATEMENTS,
    "037_whole_row_projection.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "038_sensitive_join_using.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "039_natural_join.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "040_shadowed_cte_system_relation.sql": SqlRejectionCode.FORBIDDEN_RELATION,
    "041_postgres_identity_keywords.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "042_object_identifier_cast.sql": SqlRejectionCode.FORBIDDEN_FUNCTION,
    "043_postgres_system_columns.sql": SqlRejectionCode.SENSITIVE_RAW_OUTPUT,
    "044_tablesample.sql": SqlRejectionCode.NONDETERMINISTIC_FUNCTION,
}


def _outer_limit(sql: str) -> int:
    query = sqlglot.parse_one(sql, read="postgres")
    assert isinstance(query, exp.Query)
    limit = query.args.get("limit")
    assert isinstance(limit, exp.Limit)
    assert isinstance(limit.expression, exp.Literal)
    return int(limit.expression.this)


def test_policy_fixture_sets_are_complete_and_cannot_silently_shrink() -> None:
    assert tuple(path.name for path in sorted(_GOLDEN_SQL.glob("G*.sql"))) == tuple(
        f"G{number:03d}.sql" for number in range(1, 21)
    )
    assert tuple(path.name for path in sorted(_ALLOWED_FIXTURES.glob("*.sql"))) == (
        "aggregates.sql",
        "date_trunc.sql",
        "exists.sql",
    )
    assert tuple(path.name for path in sorted(_REJECTED_FIXTURES.glob("*.sql"))) == tuple(
        _EXPECTED_REJECTIONS
    )


@pytest.mark.parametrize("path", sorted(_GOLDEN_SQL.glob("G*.sql")), ids=lambda path: path.stem)
def test_all_frozen_golden_queries_are_accepted(path: Path) -> None:
    validated = validate_sql(path.read_text(encoding="utf-8"))

    assert _outer_limit(validated.sql) <= MAX_RESULT_ROWS
    assert len(validated.query_id) == 64


@pytest.mark.parametrize(
    "path", sorted(_ALLOWED_FIXTURES.glob("*.sql")), ids=lambda path: path.stem
)
def test_allowed_analysis_fixtures_are_accepted(path: Path) -> None:
    validated = validate_sql(path.read_text(encoding="utf-8"))

    assert _outer_limit(validated.sql) == MAX_RESULT_ROWS


@pytest.mark.parametrize(
    ("filename", "expected_code"),
    tuple(_EXPECTED_REJECTIONS.items()),
    ids=lambda value: value if isinstance(value, str) else value.value,
)
def test_attack_fixtures_have_exact_rejections_without_echoing_sql(
    filename: str, expected_code: SqlRejectionCode
) -> None:
    path = _REJECTED_FIXTURES / filename
    sql = path.read_text(encoding="utf-8")

    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is expected_code
    assert sql not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", SqlRejectionCode.EMPTY_SQL),
        ("select 1; select 2", SqlRejectionCode.MULTIPLE_STATEMENTS),
        ("select pg_sleep(1)", SqlRejectionCode.FORBIDDEN_FUNCTION),
        ("select current_date", SqlRejectionCode.NONDETERMINISTIC_FUNCTION),
        ("select * from orders", SqlRejectionCode.SELECT_STAR),
        ("select customer_code from customers", SqlRejectionCode.SENSITIVE_RAW_OUTPUT),
        ("select 1 fetch first 1 rows with ties", SqlRejectionCode.WITH_TIES),
    ],
)
def test_rejection_codes_are_stable(sql: str, code: SqlRejectionCode) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is code
    assert str(raised.value) == f"SQL rejected: {code.value}"


@pytest.mark.parametrize(
    ("sql", "expected_limit"),
    [
        ("select category_id from categories", 500),
        ("select category_id from categories limit 0", 0),
        ("select category_id from categories limit 500", 500),
        ("select category_id from categories limit 501", 500),
        ("select category_id from categories limit all", 500),
        ("select category_id from categories limit $1", 500),
        ("select category_id from categories offset 3", 500),
    ],
)
def test_outer_row_limit_is_always_literal_and_bounded(sql: str, expected_limit: int) -> None:
    assert _outer_limit(validate_sql(sql).sql) == expected_limit


def test_safe_aggregate_of_sensitive_identifier_is_not_raw_disclosure() -> None:
    validated = validate_sql("select count(customer_code) as customer_count from customers")

    assert _outer_limit(validated.sql) == MAX_RESULT_ROWS


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) as matches from customers where customer_code = 'CUST-000001'",
        "select count(*) as matches from customers where customer_code like 'CUST-0000%'",
        "select count(*) as matches from customers group by customer_code",
        "select count(case when customer_code = 'x' then 1 end) as matches from customers",
        "select count(distinct customer_code) as matches from customers",
        "select count(*) as matches from customers c join orders o "
        "on c.customer_code = o.order_code",
    ],
)
def test_sensitive_identifiers_cannot_be_used_as_aggregate_side_channels(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.SENSITIVE_RAW_OUTPUT


@pytest.mark.parametrize(
    "sql",
    [
        "select o from orders as o limit 1",
        "select orders from orders limit 1",
        "select (o).order_code from orders as o limit 1",
        "select count(o) from orders as o",
        "with safe_rows as (select order_id from orders) select safe_rows from safe_rows",
        "select order_id, (select o) as leaked from orders as o limit 1",
        "select order_id, (select cast(o as text)) as leaked from orders as o limit 1",
        "select order_id, (select (select o)) as leaked from orders as o limit 1",
    ],
)
def test_whole_row_references_cannot_bypass_column_output_policy(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.SENSITIVE_RAW_OUTPUT


def test_correlated_qualified_columns_remain_allowed() -> None:
    validated = validate_sql(
        "select o.order_id from orders as o where exists ("
        "select 1 from payments as p where p.order_id = o.order_id)"
    )

    assert "p.order_id = o.order_id" in validated.sql


@pytest.mark.parametrize(
    "sql",
    [
        "select current_role",
        "select system_user",
        "select user",
        "select ctid from orders limit 1",
        "select o.xmin::text from orders as o limit 1",
        "select tableoid from orders limit 1",
    ],
)
def test_postgres_identity_and_system_columns_are_rejected(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.SENSITIVE_RAW_OUTPUT


@pytest.mark.parametrize(
    "sql",
    [
        "select 10::regrole",
        "select 'orders'::regclass",
        "select cast(10 as regnamespace)",
    ],
)
def test_postgres_object_identifier_casts_are_rejected(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.FORBIDDEN_FUNCTION


def test_cte_allowlist_is_lexically_scoped() -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(
            "select rolname from pg_roles where exists ("
            "with pg_roles as (select category_id from categories) "
            "select 1 from pg_roles)"
        )

    assert raised.value.code is SqlRejectionCode.FORBIDDEN_RELATION


def test_aggregate_that_returns_a_sensitive_value_is_rejected() -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql("select max(customer_code) as latest_customer_code from customers")

    assert raised.value.code is SqlRejectionCode.SENSITIVE_RAW_OUTPUT


def test_sensitive_cte_alias_is_not_an_output_policy_bypass() -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(
            "with leaked_values as (select customer_code as harmless_name from customers) "
            "select harmless_name from leaked_values"
        )

    assert raised.value.code is SqlRejectionCode.SENSITIVE_RAW_OUTPUT


@pytest.mark.parametrize(
    "sql",
    [
        "select region from customers union all select customer_code from customers",
        "select customer_code from customers intersect select customer_code from customers",
        (
            "select customer_code from customers except "
            "select customer_code from customers where false"
        ),
    ],
)
def test_set_operation_branches_cannot_bypass_sensitive_output_policy(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.SENSITIVE_RAW_OUTPUT


def test_query_id_is_canonical_not_source_format_dependent() -> None:
    first = validate_sql("SELECT category_id FROM categories")
    second = validate_sql(" select category_id from public.categories ")

    assert first.sql == second.sql
    assert first.query_id == second.query_id


def test_parameter_rendering_only_rewrites_placeholder_ast_nodes() -> None:
    validated = validate_sql(
        "select cast(:z as bigint) as first_value, ':z and :a' as literal_value, "
        "cast(:a as text) as second_value, cast(:z as bigint) as repeated_value"
    )

    assert validated.parameter_names == ("a", "z")
    assert validated.parameter_types == ("text", "bigint")
    assert validated.sql == (
        "SELECT CAST(:z AS BIGINT) AS first_value, ':z and :a' AS literal_value, "
        "CAST(:a AS TEXT) AS second_value, CAST(:z AS BIGINT) AS repeated_value LIMIT 500"
    )
    assert validated.driver_sql == (
        "SELECT CAST($2 AS BIGINT) AS first_value, ':z and :a' AS literal_value, "
        "CAST($1 AS TEXT) AS second_value, CAST($2 AS BIGINT) AS repeated_value LIMIT 500"
    )


@pytest.mark.parametrize(
    ("cast_type", "expected_type"),
    [
        ("text", "text"),
        ("varchar", "varchar"),
        ("varchar(12)", "varchar(12)"),
        ("char", "char(1)"),
        ("char(3)", "char(3)"),
        ("smallint", "smallint"),
        ("integer", "integer"),
        ("bigint", "bigint"),
        ("numeric", "numeric"),
        ("decimal(14, 2)", "numeric(14,2)"),
        ("boolean", "boolean"),
        ("timestamptz", "timestamptz"),
    ],
)
def test_parameter_cast_types_are_canonical_and_deterministic(
    cast_type: str, expected_type: str
) -> None:
    validated = validate_sql(f"select cast(:value as {cast_type}) as value")

    assert validated.parameter_names == ("value",)
    assert validated.parameter_types == (expected_type,)
    assert "CAST(:value AS " in validated.sql
    assert validated.driver_sql is not None
    assert "CAST($1 AS " in validated.driver_sql


def test_unqualified_char_is_canonicalized_to_explicit_char_one() -> None:
    implicit = validate_sql(
        "select cast(:value as char) as first_value, cast(:value as char(1)) as second_value"
    )
    explicit = validate_sql(
        "select cast(:value as char(1)) as first_value, "
        "cast(:value as character(1)) as second_value"
    )

    assert implicit.parameter_types == ("char(1)",)
    assert implicit.sql == explicit.sql
    assert implicit.driver_sql == explicit.driver_sql
    assert implicit.query_id == explicit.query_id


@pytest.mark.parametrize(
    "sql",
    [
        "select :value",
        "select coalesce(:value, 1)",
        "select cast(:value as date)",
        "select cast(:value as integer), cast(:value as bigint)",
        "select $1",
    ],
)
def test_parameters_without_one_consistent_supported_direct_cast_are_rejected(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.INVALID_SQL


def test_parameter_type_is_part_of_the_canonical_query_identity() -> None:
    integer = validate_sql("select cast(:value as integer)")
    bigint = validate_sql("select cast(:value as bigint)")
    decimal_alias = validate_sql("select cast(:value as decimal(14, 2))")
    numeric_alias = validate_sql("select cast(:value as numeric(14,2))")

    assert integer.query_id != bigint.query_id
    assert decimal_alias.sql == numeric_alias.sql
    assert decimal_alias.query_id == numeric_alias.query_id


def test_queries_without_parameters_have_an_empty_parameter_contract() -> None:
    validated = validate_sql("select category_id from categories")

    assert validated.parameter_names == ()
    assert validated.parameter_types == ()
    assert validated.driver_sql == validated.sql


@pytest.mark.parametrize(
    "sql",
    [
        "select order_id from orders tablesample system (10)",
        "select order_id from orders tablesample bernoulli (5) repeatable (42)",
    ],
)
def test_table_sampling_is_rejected_as_nondeterministic(sql: str) -> None:
    with pytest.raises(SqlPolicyError) as raised:
        validate_sql(sql)

    assert raised.value.code is SqlRejectionCode.NONDETERMINISTIC_FUNCTION


def test_tool_catalog_is_the_source_of_sensitive_columns() -> None:
    assert sensitive_raw_columns() == {
        "customer_code",
        "order_code",
        "payment_code",
        "refund_code",
        "session_code",
    }
