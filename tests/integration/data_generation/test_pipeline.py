"""End-to-end evidence for the bounded deterministic data-load pipeline."""

import json
from hashlib import sha256
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import pytest
from psycopg import sql

from governed_analytics.config import LoaderDatabaseSettings
from governed_analytics.data_generation.loader import (
    TABLE_LOAD_ORDER,
    TABLE_SPECS,
    _copy_from_csv,
    _psycopg_dsn,
    load_csvs,
)
from governed_analytics.data_generation.pipeline import generate_and_load
from governed_analytics.data_generation.writer import CSV_NULL, write_canonical_csv


def _scalar(statement: str) -> int:
    settings = LoaderDatabaseSettings()  # type: ignore[call-arg]
    with (
        psycopg.connect(_psycopg_dsn(settings.loader_database_url)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL search_path = public, pg_catalog")
        cursor.execute(statement)
        result = cursor.fetchone()
    assert result is not None
    return int(result[0])


def _database_digest(table_name: str) -> str:
    spec = TABLE_SPECS[table_name]
    table = sql.Identifier("public", table_name)
    columns = sql.SQL(", ").join(map(sql.Identifier, spec.columns))
    statement = sql.SQL(
        "COPY (SELECT {} FROM {} ORDER BY {}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE, NULL {})"
    ).format(
        columns, table, sql.Identifier(spec.identity_column), sql.Literal(CSV_NULL)
    )
    digest = sha256()
    settings = LoaderDatabaseSettings()  # type: ignore[call-arg]
    with (
        psycopg.connect(_psycopg_dsn(settings.loader_database_url)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL search_path = public, pg_catalog")
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        with cursor.copy(statement) as copy:
            for chunk in copy:
                digest.update(bytes(chunk))
    return digest.hexdigest()


@pytest.mark.integration
def test_tiny_pipeline_is_reproducible_and_database_backed(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = generate_and_load("data/generator/tiny.yaml", first_root)
    second = generate_and_load("data/generator/tiny.yaml", second_root)

    assert first.config_sha256 == second.config_sha256
    assert first.dataset_id == second.dataset_id
    assert tuple(item.table_name for item in first.tables) == TABLE_LOAD_ORDER
    assert [(item.table_name, item.row_count, item.sha256) for item in first.tables] == [
        (item.table_name, item.row_count, item.sha256) for item in second.tables
    ]
    artifact_names = (
        *(f"{table_name}.csv" for table_name in TABLE_LOAD_ORDER),
        "source_csv_digests.json",
        "anomaly_manifest.json",
        "dataset_manifest.json",
    )
    for name in artifact_names:
        assert (first_root / name).read_bytes() == (second_root / name).read_bytes()
    for table_name, digest in zip(TABLE_LOAD_ORDER, second.tables, strict=True):
        spec = TABLE_SPECS[table_name]
        header = (second_root / f"{table_name}.csv").read_text(encoding="utf-8").splitlines()[0]
        assert header.split(",") == list(spec.columns)
        assert digest.row_count == _scalar(f"SELECT count(*) FROM public.{table_name}")
        assert _scalar(
            f"SELECT count(*) FROM public.{table_name} "
            f"WHERE {spec.identity_column} NOT BETWEEN 1 "
            f"AND (SELECT count(*) FROM public.{table_name})"
        ) == 0

    orders_digest = next(item.sha256 for item in second.tables if item.table_name == "orders")
    assert orders_digest == _database_digest("orders")
    assert orders_digest != next(
        item["sha256"]
        for item in json.loads((second_root / "source_csv_digests.json").read_text())
        if item["table_name"] == "orders"
    )
    assert _scalar("SELECT count(*) FROM public.orders WHERE region IS NULL") == 10
    assert _scalar(
        "SELECT count(*) FROM ("
        "SELECT source_line_id FROM public.order_items GROUP BY source_line_id HAVING count(*) = 2"
        ") duplicates"
    ) == 20
    assert _scalar(
        "SELECT count(*) FROM public.order_items item "
        "LEFT JOIN public.orders orders ON orders.order_id = item.order_id "
        "WHERE orders.order_id IS NULL"
    ) == 0


@pytest.mark.integration
def test_failed_copy_rolls_back_to_prior_dataset(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    generate_and_load("data/generator/tiny.yaml", root)
    prior_categories = _scalar("SELECT count(*) FROM public.categories")
    csv_paths = {table_name: root / f"{table_name}.csv" for table_name in TABLE_LOAD_ORDER}
    (root / "categories.csv").write_text("incorrect\n", encoding="utf-8")

    with pytest.raises(ValueError, match="header"):
        load_csvs(csv_paths)

    assert _scalar("SELECT count(*) FROM public.categories") == prior_categories


@pytest.mark.integration
def test_copy_preserves_empty_text_and_null_sentinel_in_one_rolled_back_transaction(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "pipeline_runs.csv"
    frame = pd.DataFrame(
        {
            "pipeline_name": ["empty-error-code", "null-error-code"],
            "started_at": [
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:01:00Z",
            ],
            "finished_at": [None, None],
            "status": ["succeeded", "succeeded"],
            "watermark": [None, None],
            "row_count": pd.Series([None, None], dtype="Int64"),
            "error_code": ["", None],
        }
    )
    write_canonical_csv(frame, csv_path, sort_by=("pipeline_name",))

    settings = LoaderDatabaseSettings()  # type: ignore[call-arg]
    with psycopg.connect(_psycopg_dsn(settings.loader_database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = public, pg_catalog")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SELECT public.reset_analytics_dataset()")
        _copy_from_csv(connection, TABLE_SPECS["pipeline_runs"], csv_path)
        rows = connection.execute(
            """
            select pipeline_name, error_code, error_code is null
            from public.pipeline_runs
            order by pipeline_name
            """
        ).fetchall()
        assert rows == [("empty-error-code", "", False), ("null-error-code", None, True)]
        connection.rollback()
