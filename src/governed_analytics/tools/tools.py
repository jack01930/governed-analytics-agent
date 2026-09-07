"""Small, deterministic tools for schema, metrics, profiling, and SQL execution."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from governed_analytics.config import DatabaseSettings
from governed_analytics.metrics.catalog import get_metric, load_metric_catalog
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.safety.catalog import sensitive_raw_columns
from governed_analytics.safety.sql_policy import SqlPolicyError, ValidatedSql, validate_sql

from .contracts import (
    ColumnInfo,
    ErrorCode,
    ExecuteSqlRequest,
    ForeignKeyInfo,
    MetricInfo,
    MetricRequest,
    ProfileRequest,
    ProfileResult,
    QueryResult,
    SchemaRequest,
    TableInfo,
    ToolError,
    ToolResponse,
)
from .execution import AsyncEngineSqlExecutionBackend, SqlExecutionBackend

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_PATH = _REPOSITORY_ROOT / "migrations/versions/0001_create_ecommerce_schema.py"
_METRIC_PATH = _REPOSITORY_ROOT / "data/metrics/core.yaml"
_TABLE_ORDER = (
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
)
_ENUM_VALUES = {
    ("customers", "segment"): ("new", "regular", "vip"),
    ("orders", "status"): ("placed", "paid", "completed", "cancelled", "refunded"),
    ("orders", "channel"): ("organic", "search", "social", "affiliate", "email"),
    ("payments", "status"): ("pending", "succeeded", "failed"),
    ("payments", "provider"): ("alipay", "wechat_pay", "card"),
    ("refunds", "status"): ("requested", "succeeded", "rejected"),
    ("web_sessions", "channel"): ("organic", "search", "social", "affiliate", "email"),
    ("marketing_campaigns", "channel"): ("search", "social", "affiliate", "email"),
    ("pipeline_runs", "status"): ("running", "succeeded", "failed"),
}
_COLUMN_PATTERN = re.compile(
    r"^(?P<name>[a-z_]+)\s+"
    r"(?P<type>bigint|boolean|integer|numeric\([0-9, ]+\)|text|timestamptz)"
    r"(?P<tail>.*)$"
)
_TABLE_PATTERN = re.compile(r"create table (?P<name>[a-z_]+) \(\n(?P<body>.*?)\n\s*\);", re.DOTALL)
_NUMERIC_TYPE_PATTERN = re.compile(r"^numeric\((?P<precision>[0-9]+),\s*(?P<scale>[0-9]+)\)$")
_SQL_NUMERIC_TYPE_PATTERN = re.compile(r"^numeric(?:\((?P<precision>[0-9]+),(?P<scale>[0-9]+)\))?$")
_SQL_TEXT_TYPE_PATTERN = re.compile(r"^(?P<kind>text|varchar|char)(?:\((?P<length>[0-9]+)\))?$")
_CANONICAL_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_INTEGER_BOUNDS = {
    "smallint": (-(2**15), 2**15 - 1),
    "integer": (-(2**31), 2**31 - 1),
    "bigint": (-(2**63), 2**63 - 1),
}
_MAX_SQL_PARAMETER_ABS = Decimal(10**18)

type ProfileExecutor = Callable[[str, Mapping[str, object]], Awaitable[ToolResponse[QueryResult]]]


def _load_schema_registry() -> tuple[TableInfo, ...]:
    """Parse the pinned migration, never the live database or system catalogs."""
    try:
        source = _MIGRATION_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise RuntimeError("schema registry unavailable") from None
    bodies = {match.group("name"): match.group("body") for match in _TABLE_PATTERN.finditer(source)}
    if tuple(name for name in _TABLE_ORDER if name in bodies) != _TABLE_ORDER:
        raise RuntimeError("schema registry unavailable")
    tables: list[TableInfo] = []
    for table_name in _TABLE_ORDER:
        columns: list[ColumnInfo] = []
        primary_key: list[str] = []
        foreign_keys: list[ForeignKeyInfo] = []
        for raw_line in bodies[table_name].splitlines():
            matched = _COLUMN_PATTERN.match(raw_line.strip().rstrip(","))
            if matched is None:
                continue
            name = matched.group("name")
            tail = matched.group("tail")
            is_primary = "primary key" in tail
            columns.append(
                ColumnInfo(
                    name=name,
                    data_type=matched.group("type"),
                    nullable=not is_primary and "not null" not in tail,
                    allowed_values=_ENUM_VALUES.get((table_name, name), ()),
                )
            )
            if is_primary:
                primary_key.append(name)
            foreign = re.search(r"references ([a-z_]+)\(([a-z_]+)\)", tail)
            if foreign is not None:
                foreign_keys.append(
                    ForeignKeyInfo(
                        column_name=name,
                        target_table=foreign.group(1),
                        target_column=foreign.group(2),
                    )
                )
        if not columns or not primary_key:
            raise RuntimeError("schema registry unavailable")
        tables.append(
            TableInfo(
                name=table_name,
                columns=tuple(columns),
                primary_key=tuple(primary_key),
                foreign_keys=tuple(foreign_keys),
            )
        )
    return tuple(tables)


_SCHEMA = _load_schema_registry()
_TABLES = {table.name: table for table in _SCHEMA}


def _error(code: ErrorCode, message: str, *, retryable: bool = False) -> ToolResponse[Any]:
    return ToolResponse(
        ok=False,
        error=ToolError(code=code, message=message, retryable=retryable),
    )


class SchemaTool:
    """Return only the pinned public-business schema in deterministic order."""

    def run(self, request: SchemaRequest | None = None) -> ToolResponse[tuple[TableInfo, ...]]:
        requested = (request or SchemaRequest()).table_names
        if not requested:
            return ToolResponse(ok=True, data=_SCHEMA)
        unknown = set(requested) - _TABLES.keys()
        if unknown:
            return _error(ErrorCode.NOT_FOUND, "unknown table")
        requested_set = set(requested)
        return ToolResponse(
            ok=True,
            data=tuple(table for table in _SCHEMA if table.name in requested_set),
        )


def _metric_info(metric_id: str) -> MetricInfo:
    metric = get_metric(metric_id)
    return MetricInfo(**metric.model_dump(exclude={"valid_from"}))


class MetricTool:
    """Resolve exact governed metric IDs without fuzzy guessing or execution."""

    def run(self, request: MetricRequest) -> ToolResponse[MetricInfo]:
        try:
            return ToolResponse(ok=True, data=_metric_info(request.metric_id))
        except KeyError:
            return _error(ErrorCode.NOT_FOUND, "unknown metric_id")

    def list(self) -> ToolResponse[tuple[MetricInfo, ...]]:
        try:
            catalog = load_metric_catalog(_METRIC_PATH)
            return ToolResponse(
                ok=True,
                data=tuple(_metric_info(metric_id) for metric_id in catalog),
            )
        except Exception:
            return _error(ErrorCode.EXECUTION_FAILED, "metric catalog unavailable")


def _column(table: TableInfo, name: str) -> ColumnInfo | None:
    return next((column for column in table.columns if column.name == name), None)


def _profile_filter_value(column: ColumnInfo, value: str | int | float | bool) -> object:
    if column.data_type == "text":
        if not isinstance(value, str):
            raise ValueError("text filters require strings")
        return value
    if column.data_type in {"bigint", "integer"}:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("integer filters require integers")
        lower, upper = _INTEGER_BOUNDS[column.data_type]
        if not lower <= value <= upper:
            raise ValueError("integer filter is outside the database type range")
        return value
    if column.data_type.startswith("numeric("):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("numeric filters require numbers")
        parsed_type = _NUMERIC_TYPE_PATTERN.fullmatch(column.data_type)
        if parsed_type is None:
            raise ValueError("unsupported numeric profile filter type")
        precision = int(parsed_type.group("precision"))
        scale = int(parsed_type.group("scale"))
        if precision < 1:
            raise ValueError("unsupported numeric profile filter type")
        converted = Decimal(str(value))
        if not converted.is_finite():
            raise ValueError("numeric filters require finite values")
        magnitude_limit = Decimal(1).scaleb(precision - scale)
        if abs(converted) >= magnitude_limit:
            raise ValueError("numeric filter is outside the database type range")
        quantum = Decimal(1).scaleb(-scale)
        try:
            with localcontext() as context:
                context.prec = max(precision, len(converted.as_tuple().digits)) + scale + 1
                exactly_representable = converted.quantize(quantum) == converted
        except InvalidOperation:
            exactly_representable = False
        if not exactly_representable:
            raise ValueError("numeric filter has too many fractional digits")
        return converted
    if column.data_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("boolean filters require booleans")
        return value
    if column.data_type == "timestamptz":
        if not isinstance(value, str):
            raise ValueError("timestamp filters require ISO strings")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("timestamp filters require ISO strings") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp filters require timezone-aware values")
        return parsed
    raise ValueError("unsupported profile filter type")


def _profile_query(request: ProfileRequest, table: TableInfo) -> tuple[str, dict[str, object]]:
    target = _column(table, request.column_name)
    if target is None:
        raise ValueError("unknown profile column")
    if request.operation == "numeric_summary" and not target.data_type.startswith(
        ("bigint", "integer", "numeric")
    ):
        raise ValueError("numeric summary requires a numeric column")
    if request.operation == "time_range" and target.data_type != "timestamptz":
        raise ValueError("time range requires a timestamp column")
    predicates: list[str] = []
    params: dict[str, object] = {}
    for index, profile_filter in enumerate(request.filters):
        filter_column = _column(table, profile_filter.column_name)
        if filter_column is None:
            raise ValueError("unknown filter column")
        name = f"filter_{index}"
        predicates.append(
            f'"{profile_filter.column_name}" = CAST(:{name} AS {filter_column.data_type})'
        )
        params[name] = _profile_filter_value(filter_column, profile_filter.value)
    if request.time_column is not None:
        time_column = _column(table, request.time_column)
        if time_column is None or time_column.data_type != "timestamptz":
            raise ValueError("unknown time column")
        predicates.extend(
            (
                f'"{request.time_column}" >= CAST(:profile_start_at AS timestamptz)',
                f'"{request.time_column}" < CAST(:profile_end_at AS timestamptz)',
            )
        )
        params["profile_start_at"] = request.start_at
        params["profile_end_at"] = request.end_at
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    quoted_table = f'public."{request.table_name}"'
    quoted_column = f'"{request.column_name}"'
    if request.operation == "null_summary":
        sql = (
            f"SELECT count(*) FILTER (WHERE {quoted_column} IS NULL) AS null_count, "
            f"count(*) AS row_count FROM {quoted_table}{where}"
        )
    elif request.operation == "numeric_summary":
        sql = (
            f"SELECT min({quoted_column}) AS min_value, max({quoted_column}) AS max_value, "
            f"avg({quoted_column}) AS avg_value FROM {quoted_table}{where}"
        )
    elif request.operation == "time_range":
        sql = (
            f"SELECT min({quoted_column}) AS min_value, max({quoted_column}) AS max_value "
            f"FROM {quoted_table}{where}"
        )
    elif request.operation == "top_values":
        value_predicate = f"{quoted_column} IS NOT NULL"
        top_where = f"{where} AND {value_predicate}" if where else f" WHERE {value_predicate}"
        sql = (
            f"SELECT {quoted_column} AS value, count(*) AS value_count "
            f"FROM {quoted_table}{top_where} GROUP BY {quoted_column} "
            f"ORDER BY value_count DESC, value ASC LIMIT {request.limit}"
        )
    else:
        value_predicate = f"{quoted_column} IS NOT NULL"
        value_where = f"{where} AND {value_predicate}" if where else f" WHERE {value_predicate}"
        sql = (
            f"SELECT DISTINCT {quoted_column} AS value FROM {quoted_table}{value_where} "
            f"ORDER BY value ASC LIMIT {request.limit}"
        )
    return sql, params


class ProfileTool:
    """Translate structured requests into fixed, allowlisted query templates."""

    def __init__(
        self,
        execute: ProfileExecutor | None = None,
        *,
        backend: SqlExecutionBackend | None = None,
    ) -> None:
        if execute is not None and backend is not None:
            raise ValueError("choose execute or backend")
        self._execute = execute
        self._backend = backend

    async def run(self, request: ProfileRequest) -> ToolResponse[ProfileResult]:
        table = _TABLES.get(request.table_name)
        target = _column(table, request.column_name) if table is not None else None
        if table is None or target is None:
            return _error(ErrorCode.NOT_FOUND, "unknown table or column")
        sensitive_columns = sensitive_raw_columns()
        if (
            target.name in sensitive_columns
            and request.operation in {"distinct_values", "top_values"}
        ) or any(item.column_name in sensitive_columns for item in request.filters):
            return _error(
                ErrorCode.SENSITIVE_RESULT_BLOCKED,
                "sensitive values cannot be profiled",
            )
        try:
            sql, params = _profile_query(request, table)
        except ValueError:
            return _error(ErrorCode.INVALID_REQUEST, "invalid profile request")
        try:
            if self._execute is not None:
                result = await self._execute(sql, params)
            else:
                result = await _run_sql(sql, params, backend=self._backend)
        except Exception:
            return _error(ErrorCode.EXECUTION_FAILED, "profile query failed", retryable=True)
        if not result.ok or result.data is None:
            if result.error is None:
                return _error(ErrorCode.EXECUTION_FAILED, "profile query failed")
            return ToolResponse(ok=False, error=result.error)
        is_value_list = request.operation in {"distinct_values", "top_values"}
        return ToolResponse(
            ok=True,
            data=ProfileResult(
                query_id=result.data.query_id,
                table_name=request.table_name,
                column_name=request.column_name,
                operation=request.operation,
                columns=result.data.columns,
                rows=result.data.rows,
                row_count=result.data.row_count,
                value_limit=request.limit,
                possibly_truncated=is_value_list and result.data.row_count == request.limit,
            ),
        )


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError, SqlAlchemyTimeoutError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    original = error.orig
    return (
        getattr(original, "sqlstate", None) == "57014"
        or getattr(original, "pgcode", None) == "57014"
    )


def _convert_sql_parameter(parameter_type: str, value: object) -> object:
    if value is None:
        return None
    text_type = _SQL_TEXT_TYPE_PATTERN.fullmatch(parameter_type)
    if text_type is not None:
        if type(value) is not str:
            raise ValueError("text SQL parameters require strings")
        declared_length = text_type.group("length")
        if declared_length is not None and len(value) > int(declared_length):
            raise ValueError("text SQL parameter exceeds its declared length")
        return value
    if parameter_type in _INTEGER_BOUNDS:
        if type(value) is not int:
            raise ValueError("integer SQL parameters require integers")
        lower, upper = _INTEGER_BOUNDS[parameter_type]
        if not lower <= value <= upper:
            raise ValueError("integer SQL parameter is outside the database type range")
        return value
    numeric_type = _SQL_NUMERIC_TYPE_PATTERN.fullmatch(parameter_type)
    if numeric_type is not None:
        if isinstance(value, Decimal):
            converted = value
        elif type(value) in {int, float}:
            converted = Decimal(str(value))
        elif type(value) is str and _CANONICAL_DECIMAL_PATTERN.fullmatch(value) is not None:
            converted = Decimal(value)
        else:
            raise ValueError("numeric SQL parameters require numbers or decimal strings")
        if not converted.is_finite() or abs(converted) > _MAX_SQL_PARAMETER_ABS:
            raise ValueError("numeric SQL parameter must be finite and bounded")
        precision_text = numeric_type.group("precision")
        scale_text = numeric_type.group("scale")
        if precision_text is None or scale_text is None:
            return converted
        precision = int(precision_text)
        scale = int(scale_text)
        magnitude_limit = Decimal(1).scaleb(precision - scale)
        if abs(converted) >= magnitude_limit:
            raise ValueError("numeric SQL parameter is outside the database type range")
        quantum = Decimal(1).scaleb(-scale)
        try:
            with localcontext() as context:
                context.prec = max(precision, len(converted.as_tuple().digits)) + scale + 1
                exactly_representable = converted.quantize(quantum) == converted
        except InvalidOperation:
            exactly_representable = False
        if not exactly_representable:
            raise ValueError("numeric SQL parameter has too many fractional digits")
        return converted
    if parameter_type == "boolean":
        if type(value) is not bool:
            raise ValueError("boolean SQL parameters require booleans")
        return value
    if parameter_type == "timestamptz":
        if isinstance(value, datetime):
            converted_time = value
        elif type(value) is str:
            try:
                converted_time = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("timestamp SQL parameters require ISO strings") from None
        else:
            raise ValueError("timestamp SQL parameters require ISO strings")
        if converted_time.tzinfo is None or converted_time.utcoffset() is None:
            raise ValueError("timestamp SQL parameters must be timezone-aware")
        return converted_time
    raise ValueError("unsupported SQL parameter type")


def _ordered_sql_parameters(
    validated: ValidatedSql, params: Mapping[str, object]
) -> tuple[object, ...]:
    if len(validated.parameter_names) != len(validated.parameter_types):
        raise ValueError("invalid SQL parameter contract")
    return tuple(
        _convert_sql_parameter(parameter_type, params[name])
        for name, parameter_type in zip(
            validated.parameter_names, validated.parameter_types, strict=True
        )
    )


async def _execute(validated: ValidatedSql, params: tuple[object, ...]) -> QueryResult:
    engine = create_async_database_engine(DatabaseSettings())  # type: ignore[call-arg]
    try:
        return await AsyncEngineSqlExecutionBackend(engine).execute(validated, params)
    finally:
        await engine.dispose()


async def _run_sql(
    sql: str,
    params: Mapping[str, object] | None = None,
    *,
    backend: SqlExecutionBackend | None = None,
) -> ToolResponse[QueryResult]:
    try:
        validated = validate_sql(sql)
    except (SqlPolicyError, ValueError):
        return _error(ErrorCode.SQL_REJECTED, "SQL rejected by read-only policy")
    supplied = dict(params or {})
    if set(supplied) != set(validated.parameter_names):
        return _error(ErrorCode.SQL_REJECTED, "SQL parameters do not match policy")
    try:
        ordered_params = _ordered_sql_parameters(validated, supplied)
    except (TypeError, ValueError):
        return _error(ErrorCode.INVALID_REQUEST, "invalid SQL parameter value")
    try:
        result = await (
            _execute(validated, ordered_params)
            if backend is None
            else backend.execute(validated, ordered_params)
        )
        return ToolResponse(ok=True, data=result)
    except Exception as error:
        if _is_timeout(error):
            return _error(ErrorCode.QUERY_TIMEOUT, "query timed out", retryable=True)
        return _error(
            ErrorCode.EXECUTION_FAILED,
            "read-only query failed",
            retryable=True,
        )


class ExecuteSqlTool:
    """The only arbitrary read-only SQL entry point for the future agent layer."""

    def __init__(self, backend: SqlExecutionBackend | None = None) -> None:
        self._backend = backend

    async def run(self, request: ExecuteSqlRequest) -> ToolResponse[QueryResult]:
        return await _run_sql(request.sql, request.parameters, backend=self._backend)
