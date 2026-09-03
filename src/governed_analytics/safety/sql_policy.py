"""Fail-closed SQLGlot policy for the public analytics tool boundary.

This module decides whether a query is both read-only and reproducible.  It does
not execute SQL and therefore remains usable by an Execute SQL Tool before any
database connection is acquired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Final, Never

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from governed_analytics.safety.catalog import sensitive_raw_columns

MAX_RESULT_ROWS: Final = 500

_PUBLIC_TABLES: Final = frozenset(
    {
        "categories",
        "customers",
        "products",
        "orders",
        "order_items",
        "payments",
        "refunds",
        "inventory_snapshots",
        "web_sessions",
        "marketing_campaigns",
        "campaign_attributions",
        "pipeline_runs",
    }
)
_SENSITIVE_RAW_COLUMNS: Final = sensitive_raw_columns()
_POSTGRES_SYSTEM_COLUMNS: Final = frozenset({"tableoid", "xmin", "cmin", "xmax", "cmax", "ctid"})
_POSTGRES_IDENTITY_KEYWORDS: Final = frozenset({"current_role", "system_user", "user"})
_ALLOWED_DATE_TRUNC_UNITS: Final = frozenset(
    {
        "microsecond",
        "millisecond",
        "second",
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "quarter",
        "year",
    }
)
_ALLOWED_FUNCTION_TYPES: Final = (
    exp.Abs,
    exp.And,
    exp.Avg,
    exp.Case,
    exp.Cast,
    exp.Coalesce,
    exp.Count,
    exp.Exists,
    exp.If,
    exp.Max,
    exp.Min,
    exp.Nullif,
    exp.Or,
    exp.Sum,
    exp.TimestampTrunc,
)
_NONDETERMINISTIC_FUNCTION_TYPES: Final = (
    exp.CurrentDate,
    exp.CurrentTimestamp,
    exp.Rand,
    exp.TableSample,
)
_ALLOWED_CAST_TYPES: Final = frozenset(
    {
        exp.DataType.Type.BIGINT,
        exp.DataType.Type.BOOLEAN,
        exp.DataType.Type.CHAR,
        exp.DataType.Type.DATE,
        exp.DataType.Type.DECIMAL,
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.FLOAT,
        exp.DataType.Type.INT,
        exp.DataType.Type.INTERVAL,
        exp.DataType.Type.SMALLINT,
        exp.DataType.Type.TEXT,
        exp.DataType.Type.TIME,
        exp.DataType.Type.TIMESTAMP,
        exp.DataType.Type.TIMESTAMPTZ,
        exp.DataType.Type.TIMETZ,
        exp.DataType.Type.TINYINT,
        exp.DataType.Type.UUID,
        exp.DataType.Type.VARCHAR,
    }
)


class SqlRejectionCode(StrEnum):
    """Public, non-sensitive reason categories for rejected SQL."""

    EMPTY_SQL = "empty_sql"
    INVALID_SQL = "invalid_sql"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NOT_READONLY_QUERY = "not_readonly_query"
    FORBIDDEN_STATEMENT = "forbidden_statement"
    FORBIDDEN_RELATION = "forbidden_relation"
    FORBIDDEN_FUNCTION = "forbidden_function"
    NONDETERMINISTIC_FUNCTION = "nondeterministic_function"
    SELECT_STAR = "select_star"
    SENSITIVE_RAW_OUTPUT = "sensitive_raw_output"
    WITH_TIES = "with_ties"


class SqlPolicyError(ValueError):
    """Safe rejection that never includes user supplied SQL or database detail."""

    def __init__(self, code: SqlRejectionCode) -> None:
        self.code = code
        super().__init__(f"SQL rejected: {code.value}")


@dataclass(frozen=True)
class ValidatedSql:
    """Canonical SQL ready for the database-only execution boundary."""

    sql: str
    query_id: str
    driver_sql: str | None = None
    row_limit: int = MAX_RESULT_ROWS
    parameter_names: tuple[str, ...] = ()
    parameter_types: tuple[str, ...] = ()


def _reject(code: SqlRejectionCode) -> Never:
    raise SqlPolicyError(code) from None


def _parse_single_query(sql: str) -> exp.Query:
    if type(sql) is not str or not sql.strip():
        _reject(SqlRejectionCode.EMPTY_SQL)
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        _reject(SqlRejectionCode.INVALID_SQL)
    if len(statements) != 1:
        _reject(SqlRejectionCode.MULTIPLE_STATEMENTS)
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        _reject(SqlRejectionCode.NOT_READONLY_QUERY)
    return statement


def _forbidden_node_types() -> tuple[type[exp.Expression], ...]:
    names = (
        "Alter",
        "Attach",
        "Cache",
        "Command",
        "Comment",
        "Copy",
        "Create",
        "Delete",
        "Detach",
        "Drop",
        "Grant",
        "Insert",
        "Into",
        "LoadData",
        "Lock",
        "Merge",
        "Revoke",
        "Set",
        "Transaction",
        "Commit",
        "Rollback",
        "TruncateTable",
        "Uncache",
        "Update",
        "Use",
    )
    return tuple(
        node_type for name in names if isinstance((node_type := getattr(exp, name, None)), type)
    )


_FORBIDDEN_NODE_TYPES = _forbidden_node_types()


def _validate_statement_nodes(query: exp.Query) -> None:
    if any(isinstance(node, _FORBIDDEN_NODE_TYPES) for node in query.walk()):
        _reject(SqlRejectionCode.FORBIDDEN_STATEMENT)


def _validate_relations(query: exp.Query) -> None:
    """Resolve each relation in its lexical scope before applying the allowlist.

    A global CTE-name set is unsafe because an inner CTE does not shadow an
    identically named outer relation.  ``selected_sources`` distinguishes a CTE
    or derived scope from a physical table at the exact reference site.
    """

    try:
        scopes = tuple(traverse_scope(query))
        selected_sources = tuple(
            source for scope in scopes for _alias, (_node, source) in scope.selected_sources.items()
        )
    except Exception:
        _reject(SqlRejectionCode.INVALID_SQL)
    for source in selected_sources:
        if isinstance(source, Scope):
            continue
        if not isinstance(source, exp.Table):
            _reject(SqlRejectionCode.FORBIDDEN_RELATION)
        table_name = source.name.lower()
        database = source.db.lower() if source.db else ""
        catalog = source.catalog.lower() if source.catalog else ""
        if catalog or (database and database != "public") or table_name not in _PUBLIC_TABLES:
            _reject(SqlRejectionCode.FORBIDDEN_RELATION)


def _validate_no_whole_row_references(query: exp.Query) -> None:
    """Reject PostgreSQL composite-row references such as ``SELECT o``.

    Whole-row values bypass column-name checks, are not a stable JSON scalar,
    and can disclose every sensitive field of a table in one value.  A correlated
    subquery may reference a source alias from any ancestor scope, so each scope's
    own external columns must also be checked against all visible ancestors.
    """

    try:
        scopes = tuple(traverse_scope(query))
        for scope in scopes:
            local_source_names = {name.lower() for name in scope.selected_sources}
            ancestor_source_names: set[str] = set()
            ancestor = scope.parent
            while ancestor is not None:
                ancestor_source_names.update(name.lower() for name in ancestor.selected_sources)
                ancestor = ancestor.parent
            external_column_ids = {id(column) for column in scope.external_columns}
            expression = scope.expression

            def prune_nested_query(child: exp.Expr, root: exp.Expr = expression) -> bool:
                return child is not root and isinstance(child, exp.Query)

            for node in expression.walk(prune=prune_nested_query):
                if (
                    isinstance(node, exp.Column)
                    and not node.table
                    and (
                        node.name.lower() in local_source_names
                        or (
                            id(node) in external_column_ids
                            and node.name.lower() in ancestor_source_names
                        )
                    )
                ):
                    _reject(SqlRejectionCode.SENSITIVE_RAW_OUTPUT)
    except SqlPolicyError:
        raise
    except Exception:
        _reject(SqlRejectionCode.INVALID_SQL)


def _validate_join_keys(query: exp.Query) -> None:
    """Reject implicit joins and sensitive identifiers hidden in ``USING``."""

    for join in query.find_all(exp.Join):
        if str(join.args.get("method", "")).upper() == "NATURAL":
            _reject(SqlRejectionCode.SENSITIVE_RAW_OUTPUT)
        using = join.args.get("using") or ()
        if any(
            isinstance(identifier, exp.Identifier)
            and identifier.name.lower() in _SENSITIVE_RAW_COLUMNS
            for identifier in using
        ):
            _reject(SqlRejectionCode.SENSITIVE_RAW_OUTPUT)


def _validate_postgres_metadata(query: exp.Query) -> None:
    """Block implicit system columns and identity keywords not modeled as functions."""

    for column in query.find_all(exp.Column):
        name = column.name.lower()
        identifier = column.this
        if name in _POSTGRES_SYSTEM_COLUMNS or (
            not column.table
            and name in _POSTGRES_IDENTITY_KEYWORDS
            and isinstance(identifier, exp.Identifier)
            and not identifier.args.get("quoted", False)
        ):
            _reject(SqlRejectionCode.SENSITIVE_RAW_OUTPUT)


def _validate_casts(query: exp.Query) -> None:
    """Allow only scalar analytics casts, never PostgreSQL object-lookup types."""

    for cast in query.find_all(exp.Cast):
        target = cast.args.get("to")
        if not isinstance(target, exp.DataType) or target.this not in _ALLOWED_CAST_TYPES:
            _reject(SqlRejectionCode.FORBIDDEN_FUNCTION)


def _validate_functions(query: exp.Query) -> None:
    for node in query.walk():
        if isinstance(node, _NONDETERMINISTIC_FUNCTION_TYPES):
            _reject(SqlRejectionCode.NONDETERMINISTIC_FUNCTION)
        if isinstance(node, exp.TimestampTrunc):
            unit = str(node.args.get("unit", "")).lower()
            if unit not in _ALLOWED_DATE_TRUNC_UNITS:
                _reject(SqlRejectionCode.FORBIDDEN_FUNCTION)
        if isinstance(node, exp.Func) and not isinstance(node, _ALLOWED_FUNCTION_TYPES):
            _reject(SqlRejectionCode.FORBIDDEN_FUNCTION)


def _is_count_star(star: exp.Star) -> bool:
    return isinstance(star.parent, exp.Count)


def _validate_no_select_star(query: exp.Query) -> None:
    if any(not _is_count_star(star) for star in query.find_all(exp.Star)):
        _reject(SqlRejectionCode.SELECT_STAR)


def _is_raw_sensitive_projection(expression: exp.Expression) -> bool:
    """Return whether a final projection exposes a sensitive source value.

    Aggregate expressions are safe summaries, whereas casts, conditionals, and
    arbitrary arithmetic of an identifier are still raw disclosure.
    """

    if isinstance(expression, exp.Count):
        return False
    if isinstance(expression, exp.AggFunc):
        return any(_is_raw_sensitive_projection(child) for child in expression.iter_expressions())
    if isinstance(expression, exp.Column):
        return expression.name.lower() in _SENSITIVE_RAW_COLUMNS
    return any(_is_raw_sensitive_projection(child) for child in expression.iter_expressions())


def _is_direct_count_projection(column: exp.Column) -> bool:
    """Allow only ``count(sensitive_column)`` as a coarse aggregate.

    Sensitive identifiers in predicates, grouping, joins, conditions, casts, or
    distinct counts can otherwise become exact-value and prefix-existence side
    channels even when the final output is numeric.
    """

    count = column.parent
    if not isinstance(count, exp.Count) or count.this is not column:
        return False
    projection: exp.Expression = count
    if isinstance(count.parent, exp.Alias):
        projection = count.parent
    select = projection.parent
    return isinstance(select, exp.Select) and projection in select.expressions


def _validate_output_policy(query: exp.Query) -> None:
    """Reject raw sensitive projections in every result-producing branch.

    A set operation has more than one final projection, and CTE/subquery aliases
    otherwise make source-column checks easy to evade.  Inspecting every SELECT
    is deliberately conservative: a sensitive raw value must never enter a
    derived relation unless it is already reduced by COUNT.
    """

    selects = tuple(query.find_all(exp.Select))
    if not selects:
        _reject(SqlRejectionCode.NOT_READONLY_QUERY)
    if any(
        column.name.lower() in _SENSITIVE_RAW_COLUMNS and not _is_direct_count_projection(column)
        for column in query.find_all(exp.Column)
    ):
        _reject(SqlRejectionCode.SENSITIVE_RAW_OUTPUT)
    for select in selects:
        for projection in select.expressions:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            if _is_raw_sensitive_projection(expression):
                _reject(SqlRejectionCode.SENSITIVE_RAW_OUTPUT)


def _has_with_ties(query: exp.Query) -> bool:
    return any(
        isinstance(node, exp.Fetch)
        and isinstance(node.args.get("limit_options"), exp.LimitOptions)
        and node.args["limit_options"].args.get("with_ties") is True
        for node in query.walk()
    )


def _safe_outer_cap(row_cap: exp.Limit | exp.Fetch) -> bool:
    expression = row_cap.expression if isinstance(row_cap, exp.Limit) else row_cap.args.get("count")
    if not isinstance(expression, exp.Literal) or expression.is_string:
        return False
    try:
        return 0 <= int(expression.this) <= MAX_RESULT_ROWS
    except (TypeError, ValueError):
        return False


def _apply_outer_row_limit(query: exp.Query) -> None:
    if _has_with_ties(query):
        _reject(SqlRejectionCode.WITH_TIES)
    row_cap = query.args.get("limit")
    if isinstance(row_cap, (exp.Limit, exp.Fetch)) and _safe_outer_cap(row_cap):
        return
    query.set("limit", exp.Limit(expression=exp.Literal.number(MAX_RESULT_ROWS)))


def _canonicalize_public_schema(query: exp.Query) -> None:
    """Keep public-qualified and unqualified references at one audit identity."""

    for table in query.find_all(exp.Table):
        if table.db.lower() == "public" and not table.catalog:
            table.set("db", None)


def _data_type_parameters(data_type: exp.DataType) -> tuple[int, ...]:
    parameters: list[int] = []
    for parameter in data_type.expressions:
        if not isinstance(parameter, exp.DataTypeParam) or not isinstance(
            parameter.this, exp.Literal
        ):
            _reject(SqlRejectionCode.INVALID_SQL)
        literal = parameter.this
        if literal.is_string or not str(literal.this).isdigit():
            _reject(SqlRejectionCode.INVALID_SQL)
        parameters.append(int(literal.this))
    return tuple(parameters)


def _canonical_parameter_type(data_type: exp.DataType) -> str:
    parameters = _data_type_parameters(data_type)
    base_type = data_type.this
    if base_type is exp.DataType.Type.TEXT:
        if parameters:
            _reject(SqlRejectionCode.INVALID_SQL)
        return "text"
    if base_type in {exp.DataType.Type.VARCHAR, exp.DataType.Type.CHAR}:
        if len(parameters) > 1 or (parameters and parameters[0] < 1):
            _reject(SqlRejectionCode.INVALID_SQL)
        name = "varchar" if base_type is exp.DataType.Type.VARCHAR else "char"
        if base_type is exp.DataType.Type.CHAR and not parameters:
            parameters = (1,)
            data_type.set(
                "expressions",
                [exp.DataTypeParam(this=exp.Literal.number(parameters[0]))],
            )
        return f"{name}({parameters[0]})" if parameters else name
    if base_type in {
        exp.DataType.Type.SMALLINT,
        exp.DataType.Type.INT,
        exp.DataType.Type.BIGINT,
    }:
        if parameters:
            _reject(SqlRejectionCode.INVALID_SQL)
        return {
            exp.DataType.Type.SMALLINT: "smallint",
            exp.DataType.Type.INT: "integer",
            exp.DataType.Type.BIGINT: "bigint",
        }[base_type]
    if base_type is exp.DataType.Type.DECIMAL:
        if len(parameters) > 2:
            _reject(SqlRejectionCode.INVALID_SQL)
        if parameters:
            precision = parameters[0]
            scale = parameters[1] if len(parameters) == 2 else 0
            if not 1 <= precision <= 1000 or scale > precision:
                _reject(SqlRejectionCode.INVALID_SQL)
            return f"numeric({precision},{scale})"
        return "numeric"
    if base_type is exp.DataType.Type.BOOLEAN:
        if parameters:
            _reject(SqlRejectionCode.INVALID_SQL)
        return "boolean"
    if base_type is exp.DataType.Type.TIMESTAMPTZ:
        if parameters:
            _reject(SqlRejectionCode.INVALID_SQL)
        return "timestamptz"
    _reject(SqlRejectionCode.INVALID_SQL)


def _parameter_contract(query: exp.Query) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if any(isinstance(node, exp.Parameter) for node in query.walk()):
        _reject(SqlRejectionCode.INVALID_SQL)
    types_by_name: dict[str, str] = {}
    for placeholder in query.find_all(exp.Placeholder):
        name = placeholder.name
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
            _reject(SqlRejectionCode.INVALID_SQL)
        cast = placeholder.parent
        if not isinstance(cast, exp.Cast) or cast.this is not placeholder:
            _reject(SqlRejectionCode.INVALID_SQL)
        target = cast.args.get("to")
        if not isinstance(target, exp.DataType):
            _reject(SqlRejectionCode.INVALID_SQL)
        parameter_type = _canonical_parameter_type(target)
        previous_type = types_by_name.setdefault(name, parameter_type)
        if previous_type != parameter_type:
            _reject(SqlRejectionCode.INVALID_SQL)
    names = tuple(sorted(types_by_name))
    return names, tuple(types_by_name[name] for name in names)


def _render_parameterized_sql(
    query: exp.Query, parameter_names: tuple[str, ...]
) -> tuple[str, str]:
    """Render separate audit and driver SQL without touching string literals.

    SQLGlot renders named PostgreSQL placeholders as ``%(name)s`` while the
    asyncpg driver expects positional ``$n`` parameters.  Both representations
    are created by replacing only Placeholder AST nodes; global text replacement
    would corrupt a legitimate literal containing the same bytes.
    """

    positions = {name: index for index, name in enumerate(parameter_names, start=1)}
    audit_query = query.copy()
    for placeholder in tuple(audit_query.find_all(exp.Placeholder)):
        placeholder.replace(exp.Var(this=f":{placeholder.name}"))
    driver_query = query.copy()
    for placeholder in tuple(driver_query.find_all(exp.Placeholder)):
        placeholder.replace(exp.Var(this=f"${positions[placeholder.name]}"))
    if any(audit_query.find_all(exp.Placeholder)) or any(driver_query.find_all(exp.Placeholder)):
        _reject(SqlRejectionCode.INVALID_SQL)
    return (
        audit_query.sql(dialect="postgres", pretty=False, normalize=True),
        driver_query.sql(dialect="postgres", pretty=False, normalize=True),
    )


def validate_sql(sql: str) -> ValidatedSql:
    """Return one canonical, public-schema, deterministic, capped SQL query.

    The resulting statement still must run in the separate read-only database
    transaction.  Policy validation never opens a connection.
    """

    query = _parse_single_query(sql)
    _validate_statement_nodes(query)
    _validate_relations(query)
    _validate_no_whole_row_references(query)
    _validate_join_keys(query)
    _validate_postgres_metadata(query)
    _validate_casts(query)
    _validate_functions(query)
    _validate_no_select_star(query)
    _validate_output_policy(query)
    _apply_outer_row_limit(query)
    parameter_names, parameter_types = _parameter_contract(query)
    _canonicalize_public_schema(query)
    canonical_sql, driver_sql = _render_parameterized_sql(query, parameter_names)
    return ValidatedSql(
        sql=canonical_sql,
        query_id=sha256(canonical_sql.encode("utf-8")).hexdigest(),
        driver_sql=driver_sql,
        parameter_names=parameter_names,
        parameter_types=parameter_types,
    )
