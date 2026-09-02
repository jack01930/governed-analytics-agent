"""Canonical, byte-stable artifact writers for generated datasets."""

import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]


def _format_datetime(value: datetime) -> str:
    """Serialize one UTC value to the second without accepting ambiguous timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be UTC-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime values must use UTC")
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_value(value: object) -> object:
    """Return a CSV-safe scalar while preserving the dataset type contract."""
    if value is None or value is pd.NA or value is pd.NaT:
        return ""
    if isinstance(value, pd.Timestamp):
        return _format_datetime(value.to_pydatetime())
    if isinstance(value, datetime):
        return _format_datetime(value)
    if isinstance(value, Decimal):
        rounded = value.quantize(Decimal("0.01"))
        if rounded != value:
            raise ValueError("Decimal values must have at most two decimal places")
        return format(rounded, ".2f")
    if isinstance(value, bool):
        return "True" if value else "False"
    if pd.isna(value):
        return ""
    return value


def write_canonical_csv(
    frame: pd.DataFrame,
    path: Path,
    *,
    sort_by: tuple[str, ...],
    columns: tuple[str, ...] | None = None,
) -> str:
    """Write a stable UTF-8 RFC 4180 CSV and return its SHA-256 digest.

    Values are formatted directly instead of delegated to ``DataFrame.to_csv`` so nullable
    integer, Decimal and UTC timestamp representations cannot be changed by pandas inference.
    """
    if not sort_by or any(column not in frame.columns for column in sort_by):
        raise ValueError("sort_by must contain only existing columns")
    if frame.columns.has_duplicates:
        raise ValueError("CSV columns must be unique")
    output_columns = tuple(frame.columns) if columns is None else columns
    if not output_columns or any(column not in frame.columns for column in output_columns):
        raise ValueError("columns must contain only existing columns")
    if len(set(output_columns)) != len(output_columns):
        raise ValueError("CSV output columns must be unique")

    canonical = frame.sort_values(list(sort_by), kind="stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        output = csv.writer(target, lineterminator="\n")
        output.writerow(output_columns)
        for row in canonical.loc[:, output_columns].itertuples(index=False, name=None):
            output.writerow([_format_value(value) for value in row])
    return sha256(path.read_bytes()).hexdigest()


def write_canonical_json(value: Any, path: Path) -> str:
    """Write an indented, Unicode-preserving JSON document with a trailing LF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, default=str) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")
    return sha256(payload.encode("utf-8")).hexdigest()
