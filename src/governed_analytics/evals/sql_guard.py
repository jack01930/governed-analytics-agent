"""A deliberately narrow, AST-based guard for Week 1 generated SQL."""

from __future__ import annotations

from typing import Never

import sqlglot
from sqlglot import exp

_MAX_RESULT_ROWS = 500
_DANGEROUS_FUNCTIONS = frozenset(
    {
        "dblink",
        "lo_export",
        "lo_import",
        "pg_ls_dir",
        "pg_read_file",
        "pg_sleep",
        "set_config",
    }
)
_FORBIDDEN_NODE_TYPES = (
    exp.Alter,
    exp.Attach,
    exp.Cache,
    exp.Command,
    exp.Comment,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Detach,
    exp.Drop,
    exp.Grant,
    exp.Insert,
    exp.Into,
    exp.LoadData,
    exp.Lock,
    exp.Merge,
    exp.Revoke,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.TruncateTable,
    exp.Uncache,
    exp.Update,
    exp.Use,
)


class SqlRejected(ValueError):
    """Stable public error for SQL that is outside the Week 1 baseline boundary."""


def _reject() -> Never:
    raise SqlRejected("baseline SQL rejected") from None


def _parse_single_query(sql: str) -> exp.Query:
    if type(sql) is not str or not sql.strip():
        _reject()
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        _reject()
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        _reject()
    return statements[0]


def _has_forbidden_content(query: exp.Query) -> bool:
    for node in query.walk():
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            return True
        if isinstance(node, exp.Func) and node.name.lower() in _DANGEROUS_FUNCTIONS:
            return True
    return False


def _outer_row_cap_is_safe_literal(row_cap: exp.Limit | exp.Fetch) -> bool:
    expression = (
        row_cap.expression if isinstance(row_cap, exp.Limit) else row_cap.args.get("count")
    )
    if not isinstance(expression, exp.Literal) or expression.is_string:
        return False
    try:
        return 0 <= int(expression.this) <= _MAX_RESULT_ROWS
    except (TypeError, ValueError):
        return False


def _cap_outer_limit(query: exp.Query) -> None:
    row_cap = query.args.get("limit")
    if isinstance(row_cap, (exp.Limit, exp.Fetch)) and _outer_row_cap_is_safe_literal(row_cap):
        return
    query.set("limit", exp.Limit(expression=exp.Literal.number(_MAX_RESULT_ROWS)))


def validate_baseline_sql(sql: str) -> str:
    """Validate one read-only PostgreSQL query and enforce a 500-row outer cap."""
    query = _parse_single_query(sql)
    if _has_forbidden_content(query):
        _reject()
    _cap_outer_limit(query)
    return query.sql(dialect="postgres", pretty=False, normalize=True)
