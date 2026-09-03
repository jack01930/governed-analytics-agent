"""Validated, non-secret visibility metadata for safety policy."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_CATALOG_PATH = Path(__file__).resolve().parents[3] / "data" / "tool_catalog.yaml"


@lru_cache(maxsize=1)
def sensitive_raw_columns() -> frozenset[str]:
    """Return the fixed set of columns that may not be emitted as raw values."""

    try:
        raw: Any = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8"))
        tables = raw["tables"]
        if raw.get("version") != 1 or not isinstance(tables, dict):
            raise ValueError
        columns = {
            column.lower()
            for table, entries in tables.items()
            if isinstance(table, str) and isinstance(entries, dict)
            for column, visibility in entries.items()
            if isinstance(column, str) and visibility == "sensitive"
        }
        if not columns:
            raise ValueError
    except (OSError, TypeError, ValueError, yaml.YAMLError, KeyError):
        raise RuntimeError("tool catalog unavailable") from None
    return frozenset(columns)
