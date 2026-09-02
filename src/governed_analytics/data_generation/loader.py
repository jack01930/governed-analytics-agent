"""Bounded PostgreSQL COPY loader for the deterministic dataset contract."""

import csv
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from governed_analytics.config import LoaderDatabaseSettings
from governed_analytics.data_generation.models import TableDigest

_COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TableSpec:
    """One schema-owned table admitted to this loader's fixed write boundary."""

    table_name: str
    identity_column: str
    columns: tuple[str, ...]
    sort_by: tuple[str, ...]


TABLE_LOAD_ORDER = (
    "categories",
    "customers",
    "products",
    "marketing_campaigns",
    "orders",
    "order_items",
    "payments",
    "refunds",
    "inventory_snapshots",
    "web_sessions",
    "campaign_attributions",
    "pipeline_runs",
)

TABLE_SPECS: Mapping[str, TableSpec] = MappingProxyType(
    {
        "categories": TableSpec(
            "categories",
            "category_id",
            ("category_code", "category_name", "created_at"),
            ("category_id",),
        ),
        "customers": TableSpec(
            "customers",
            "customer_id",
            ("customer_code", "segment", "region", "registered_at"),
            ("customer_id",),
        ),
        "products": TableSpec(
            "products",
            "product_id",
            ("sku", "category_id", "product_name", "list_price", "unit_cost", "is_active"),
            ("product_id",),
        ),
        "marketing_campaigns": TableSpec(
            "marketing_campaigns",
            "campaign_id",
            ("campaign_code", "campaign_name", "channel", "start_at", "end_at", "spend"),
            ("campaign_id",),
        ),
        "orders": TableSpec(
            "orders",
            "order_id",
            (
                "order_code",
                "customer_id",
                "status",
                "ordered_at",
                "region",
                "channel",
                "currency",
                "gross_amount",
                "discount_amount",
                "shipping_amount",
                "payable_amount",
                "updated_at",
            ),
            ("order_id",),
        ),
        "order_items": TableSpec(
            "order_items",
            "order_item_id",
            (
                "source_line_id",
                "order_id",
                "product_id",
                "quantity",
                "unit_price",
                "discount_amount",
                "gross_amount",
                "net_amount",
            ),
            ("order_item_id",),
        ),
        "payments": TableSpec(
            "payments",
            "payment_id",
            ("payment_code", "order_id", "status", "provider", "amount", "paid_at", "created_at"),
            ("payment_id",),
        ),
        "refunds": TableSpec(
            "refunds",
            "refund_id",
            (
                "refund_code",
                "order_id",
                "order_item_id",
                "status",
                "amount",
                "reason",
                "refunded_at",
                "created_at",
            ),
            ("refund_id",),
        ),
        "inventory_snapshots": TableSpec(
            "inventory_snapshots",
            "inventory_snapshot_id",
            ("snapshot_at", "product_id", "available_qty", "reserved_qty"),
            ("inventory_snapshot_id",),
        ),
        "web_sessions": TableSpec(
            "web_sessions",
            "session_id",
            (
                "session_code",
                "customer_id",
                "order_id",
                "channel",
                "occurred_at",
                "converted",
                "duration_seconds",
            ),
            ("session_id",),
        ),
        "campaign_attributions": TableSpec(
            "campaign_attributions",
            "attribution_id",
            ("campaign_id", "order_id", "attributed_revenue", "attributed_at"),
            ("attribution_id",),
        ),
        "pipeline_runs": TableSpec(
            "pipeline_runs",
            "pipeline_run_id",
            (
                "pipeline_name",
                "started_at",
                "finished_at",
                "status",
                "watermark",
                "row_count",
                "error_code",
            ),
            ("pipeline_run_id",),
        ),
    }
)


def table_spec(table_name: str) -> TableSpec:
    """Return one static specification and reject all unregistered identifiers."""
    try:
        return TABLE_SPECS[table_name]
    except KeyError as error:
        raise ValueError(f"unknown registry table: {table_name}") from error


def _psycopg_dsn(database_url: str) -> str:
    """Convert the validated SQLAlchemy psycopg URL into a psycopg connection DSN."""
    parsed = urlsplit(database_url)
    if parsed.scheme != "postgresql+psycopg":
        raise ValueError("loader database URL must use postgresql+psycopg")
    return urlunsplit(("postgresql", parsed.netloc, parsed.path, parsed.query, ""))


def _validate_csv_header(csv_path: Path, spec: TableSpec) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as source:
        header = next(csv.reader(source), None)
    if header != list(spec.columns):
        raise ValueError(f"CSV header for {spec.table_name} must exactly match registry columns")


def _table_identifier(spec: TableSpec) -> sql.Identifier:
    return sql.Identifier("public", spec.table_name)


def _copy_from_csv(connection: psycopg.Connection[Any], spec: TableSpec, csv_path: Path) -> None:
    _validate_csv_header(csv_path, spec)
    statement = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)").format(
        _table_identifier(spec), sql.SQL(", ").join(map(sql.Identifier, spec.columns))
    )
    with (
        connection.cursor() as cursor,
        cursor.copy(statement) as copy,
        csv_path.open("rb") as source,
    ):
        while chunk := source.read(_COPY_CHUNK_SIZE):
            copy.write(chunk)


def _database_digest(connection: psycopg.Connection[Any], spec: TableSpec) -> TableDigest:
    table = _table_identifier(spec)
    columns = sql.SQL(", ").join(map(sql.Identifier, spec.columns))
    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        columns, table, sql.Identifier(spec.identity_column)
    )
    copy_statement = sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)").format(query)
    digest = sha256()
    with connection.cursor() as cursor, cursor.copy(copy_statement) as copy:
        for chunk in copy:
            digest.update(bytes(chunk))
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(table))
        result = cursor.fetchone()
    if result is None:
        raise RuntimeError(f"unable to count table {spec.table_name}")
    return TableDigest(
        table_name=spec.table_name,
        row_count=int(result[0]),
        sha256=digest.hexdigest(),
    )


def _verify_identity_sequence(connection: psycopg.Connection[Any], spec: TableSpec) -> None:
    statement = sql.SQL("SELECT count(*), min({}), max({}) FROM {}").format(
        sql.Identifier(spec.identity_column),
        sql.Identifier(spec.identity_column),
        _table_identifier(spec),
    )
    with connection.cursor() as cursor:
        cursor.execute(statement)
        result = cursor.fetchone()
    if result is None:
        raise RuntimeError(f"unable to verify identities for {spec.table_name}")
    count, minimum, maximum = result
    if int(count) and (int(minimum) != 1 or int(maximum) != int(count)):
        raise ValueError(f"generated identities for {spec.table_name} are not contiguous 1..N")


def _validate_paths(csv_paths: Mapping[str, Path]) -> None:
    names = tuple(csv_paths)
    if set(names) != set(TABLE_LOAD_ORDER):
        unknown = sorted(set(names) - set(TABLE_LOAD_ORDER))
        missing = sorted(set(TABLE_LOAD_ORDER) - set(names))
        raise ValueError(
            f"CSV paths must contain exactly registry tables; unknown={unknown}, missing={missing}"
        )
    for table_name in TABLE_LOAD_ORDER:
        if not csv_paths[table_name].is_file():
            raise ValueError(f"CSV file for {table_name} does not exist")


def load_csvs(csv_paths: Mapping[str, Path]) -> tuple[TableDigest, ...]:
    """Atomically replace exactly the twelve registry tables from canonical CSV files."""
    _validate_paths(csv_paths)
    settings = LoaderDatabaseSettings()  # type: ignore[call-arg]
    with (
        psycopg.connect(_psycopg_dsn(settings.loader_database_url)) as connection,
        connection.transaction(),
    ):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = public, pg_catalog")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SELECT public.reset_analytics_dataset()")
        for table_name in TABLE_LOAD_ORDER:
            _copy_from_csv(connection, table_spec(table_name), csv_paths[table_name])
        for table_name in TABLE_LOAD_ORDER:
            _verify_identity_sequence(connection, table_spec(table_name))
        return tuple(
            _database_digest(connection, table_spec(table_name)) for table_name in TABLE_LOAD_ORDER
        )
