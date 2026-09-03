"""Safe loader for the checked-in metric catalog.

The catalog is metadata, not executable SQL.  SQL parsing is used only to reject
unsafe or malformed expressions; callers must use independently fixed queries.
"""

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
import yaml  # type: ignore[import-untyped]
from sqlglot import exp

from governed_analytics.domain.metrics import MetricDefinition

_CATALOG_FIELDS = frozenset((*MetricDefinition.model_fields, "example_question"))
_SOURCE_TABLES = frozenset(
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
_DIMENSIONS = frozenset(
    {"region", "channel", "category", "product", "segment", "provider", "reason", "campaign"}
)
_CORE_CATALOG_PATH = Path(__file__).resolve().parents[3] / "data" / "metrics" / "core.yaml"


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Safe YAML loader that rejects silently overwritten mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _validate_expression(expression_sql: str) -> None:
    """Accept exactly one PostgreSQL SELECT projection without command nodes."""
    try:
        statements = sqlglot.parse(f"SELECT {expression_sql}", read="postgres")
        parsed = sqlglot.parse_one(f"SELECT {expression_sql}", read="postgres")
    except sqlglot.errors.ParseError as error:
        raise ValueError("invalid expression_sql") from error
    if (
        len(statements) != 1
        or not isinstance(parsed, exp.Select)
        or len(parsed.expressions) != 1
        or parsed.args.get("from")
        or parsed.args.get("joins")
        or parsed.args.get("into")
        or parsed.args.get("locks")
    ):
        raise ValueError("expression_sql must form a single SELECT projection")
    prohibited = tuple(
        node_type
        for name in (
            "DDL", "DML", "Command", "Into", "Lock", "Copy", "Transaction",
            "Commit", "Rollback", "Set", "Pragma",
        )
        if isinstance((node_type := getattr(exp, name, None)), type)
    )
    if any(isinstance(node, prohibited) for node in parsed.walk()):
        raise ValueError("expression_sql must form a single SELECT projection")


def _validate_raw_metric(raw: Mapping[str, Any]) -> MetricDefinition:
    keys = frozenset(raw)
    missing = _CATALOG_FIELDS - keys
    extra = keys - _CATALOG_FIELDS
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing metadata: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"extra metadata: {', '.join(sorted(extra))}")
        raise ValueError("; ".join(details))
    example_question = raw["example_question"]
    if not isinstance(example_question, str) or not example_question.strip():
        raise ValueError("example_question must be a non-empty string")
    source_tables = raw["source_tables"]
    if not isinstance(source_tables, list) or not all(
        isinstance(item, str) for item in source_tables
    ):
        raise ValueError("source_tables must be a list of strings")
    unknown_sources = set(source_tables) - _SOURCE_TABLES
    if unknown_sources:
        raise ValueError(f"unknown source table: {', '.join(sorted(unknown_sources))}")
    dimensions = raw["dimensions"]
    if not isinstance(dimensions, list) or not all(isinstance(item, str) for item in dimensions):
        raise ValueError("dimensions must be a list of strings")
    unknown_dimensions = set(dimensions) - _DIMENSIONS
    if unknown_dimensions:
        raise ValueError(f"unknown dimension: {', '.join(sorted(unknown_dimensions))}")
    expression_sql = raw["expression_sql"]
    if not isinstance(expression_sql, str):
        raise ValueError("expression_sql must be a string")
    _validate_expression(expression_sql)
    try:
        return MetricDefinition.model_validate(
            {key: value for key, value in raw.items() if key != "example_question"}
        )
    except ValueError as error:
        raise ValueError(str(error)) from error


def load_metric_catalog(path: str | Path) -> dict[str, MetricDefinition]:
    """Load a non-empty ordered YAML list of safe immutable metric definitions."""
    raw: Any = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    if not isinstance(raw, list) or not raw:
        raise ValueError("metric catalog must be a non-empty YAML list")
    catalog: dict[str, MetricDefinition] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("each metric catalog item must be a YAML mapping")
        metric = _validate_raw_metric(item)
        if metric.metric_id in catalog:
            raise ValueError(f"duplicate metric_id: {metric.metric_id}")
        catalog[metric.metric_id] = metric
    return catalog


@lru_cache(maxsize=1)
def _load_core_catalog() -> dict[str, MetricDefinition]:
    return load_metric_catalog(_CORE_CATALOG_PATH)


def get_metric(metric_id: str) -> MetricDefinition:
    """Return an installed core metric or raise an actionable lookup error."""
    catalog = _load_core_catalog()
    try:
        return catalog[metric_id]
    except KeyError as error:
        raise KeyError(f"unknown metric_id: {metric_id}") from error
