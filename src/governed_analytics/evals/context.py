"""Stable, non-secret generation context for the Week 1 baseline."""

from __future__ import annotations

import re
from pathlib import Path

from governed_analytics.metrics.catalog import load_metric_catalog

SCHEMA_CONTEXT_VERSION = "ecommerce-schema-v1"
METRIC_CONTEXT_VERSION = "metrics-v1"
EVALUATION_CONTEXT_VERSION = f"{SCHEMA_CONTEXT_VERSION}+{METRIC_CONTEXT_VERSION}"

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_PATH = _REPOSITORY_ROOT / "migrations" / "versions" / "0001_create_ecommerce_schema.py"
_METRIC_CATALOG_PATH = _REPOSITORY_ROOT / "data" / "metrics" / "core.yaml"
_PUBLIC_TABLES = (
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


def _migration_tables(source: str) -> dict[str, str]:
    return {
        match.group("name"): match.group("body")
        for match in re.finditer(
            r"create table (?P<name>[a-z_]+) \(\n(?P<body>.*?)\n\s*\);",
            source,
            flags=re.DOTALL,
        )
    }


def _column_description(line: str) -> tuple[str, str, bool, tuple[str, str, str] | None] | None:
    line = line.strip().rstrip(",")
    if not line or line.startswith("constraint "):
        return None
    matched = re.match(r"(?P<name>[a-z_]+)\s+(?P<tail>.+)", line)
    if matched is None:
        return None
    name = matched.group("name")
    tail = matched.group("tail")
    type_matched = re.match(r"([a-z]+(?:\([0-9, ]+\))?)", tail)
    if type_matched is None or type_matched.group(1).split("(", 1)[0] not in {
        "bigint",
        "boolean",
        "integer",
        "numeric",
        "text",
        "timestamptz",
    }:
        return None
    foreign = re.search(r"references ([a-z_]+)\(([a-z_]+)\)", tail)
    return (
        name,
        type_matched.group(1),
        "primary key" in tail,
        (name, foreign.group(1), foreign.group(2)) if foreign else None,
    )


def build_schema_context() -> str:
    """Render migration-0001's public schema in a fixed, answer-free order."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    tables = _migration_tables(source)
    if tuple(table for table in _PUBLIC_TABLES if table in tables) != _PUBLIC_TABLES:
        raise ValueError("schema context source is incomplete")
    lines = [f"SCHEMA VERSION {SCHEMA_CONTEXT_VERSION}"]
    foreign_keys: list[tuple[str, str, str]] = []
    for table in _PUBLIC_TABLES:
        columns: list[str] = []
        primary_keys: list[str] = []
        for raw_line in tables[table].splitlines():
            parsed = _column_description(raw_line)
            if parsed is None:
                continue
            column, column_type, is_primary, foreign = parsed
            columns.append(f"{column} {column_type}")
            if is_primary:
                primary_keys.append(column)
            if foreign is not None:
                foreign_keys.append((table, foreign[0], f"{foreign[1]}.{foreign[2]}"))
        lines.append(f"TABLE {table}: " + ", ".join(columns))
        lines.append(f"PRIMARY KEY {table}: " + ", ".join(primary_keys))
    for table, column, target in foreign_keys:
        lines.append(f"FOREIGN KEY {table}.{column} -> {target}")
    lines.extend(
        (
            "orders.status enum: placed, paid, completed, cancelled, refunded",
            "orders.channel enum: organic, search, social, affiliate, email",
            "payments.status enum: pending, succeeded, failed",
            "refunds.status enum: requested, succeeded, rejected",
            "web_sessions.channel enum: organic, search, social, affiliate, email",
            "marketing_campaigns.channel enum: search, social, affiliate, email",
            "pipeline_runs.status enum: running, succeeded, failed",
        )
    )
    return "\n".join(lines) + "\n"


def build_metric_context() -> str:
    """Render checked-in governed metric metadata, excluding example questions."""
    catalog = load_metric_catalog(_METRIC_CATALOG_PATH)
    lines = [f"METRICS VERSION {METRIC_CONTEXT_VERSION}"]
    for metric in catalog.values():
        lines.append(f"METRIC {metric.metric_id} version={metric.version}")
        lines.append(f"description: {metric.description}")
        lines.append(f"expression: {metric.expression_sql}")
        lines.append(f"time_field: {metric.time_field}")
        lines.append("default_filters: " + "; ".join(metric.default_filters))
        lines.append("dimensions: " + ", ".join(metric.dimensions))
        lines.append("source_tables: " + ", ".join(metric.source_tables))
        lines.append(f"unit: {metric.unit}")
    return "\n".join(lines) + "\n"
