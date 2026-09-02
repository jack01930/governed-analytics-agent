from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from governed_analytics.domain.metrics import MetricDefinition
from governed_analytics.metrics.catalog import get_metric, load_metric_catalog

CORE_METRIC_IDS = (
    "gmv",
    "paid_gmv",
    "net_revenue",
    "valid_order_count",
    "average_order_value",
    "payment_success_rate",
    "refund_amount",
    "refund_rate",
    "active_customers",
    "new_customers",
    "repeat_purchase_rate",
    "customer_acquisition_cost",
    "conversion_rate",
    "stockout_rate",
    "campaign_roi",
)

EXPECTED_METADATA = {
    "gmv": ("o.ordered_at", "cny", ("region", "channel", "category", "product", "segment")),
    "paid_gmv": ("p.paid_at", "cny", ("region", "channel", "segment")),
    "net_revenue": ("o.ordered_at", "cny", ("region", "channel", "category", "product")),
    "valid_order_count": ("o.ordered_at", "count", ("region", "channel", "segment")),
    "average_order_value": ("o.ordered_at", "cny", ("region", "channel", "segment")),
    "payment_success_rate": ("p.created_at", "ratio", ("provider", "channel")),
    "refund_amount": ("r.refunded_at", "cny", ("reason", "category", "product", "region")),
    "refund_rate": ("o.ordered_at", "ratio", ("category", "product", "region")),
    "active_customers": ("o.ordered_at", "count", ("region", "segment")),
    "new_customers": ("c.registered_at", "count", ("region", "segment")),
    "repeat_purchase_rate": ("o.ordered_at", "ratio", ("region", "segment")),
    "customer_acquisition_cost": ("a.attributed_at", "cny", ("campaign", "channel")),
    "conversion_rate": ("s.occurred_at", "ratio", ("channel", "region")),
    "stockout_rate": ("i.snapshot_at", "ratio", ("category", "product")),
    "campaign_roi": ("a.attributed_at", "ratio", ("campaign", "channel")),
}


def test_metric_definition_exposes_only_the_authoritative_fields() -> None:
    assert tuple(MetricDefinition.model_fields) == (
        "metric_id",
        "name_zh",
        "name_en",
        "description",
        "expression_sql",
        "time_field",
        "default_filters",
        "dimensions",
        "source_tables",
        "unit",
        "version",
        "valid_from",
    )


def test_core_catalog_has_authoritative_ids_and_contracts() -> None:
    catalog = load_metric_catalog("data/metrics/core.yaml")

    assert tuple(catalog) == CORE_METRIC_IDS
    assert "session_count" not in catalog
    assert catalog["customer_acquisition_cost"].unit == "cny"
    assert catalog["customer_acquisition_cost"].time_field == "a.attributed_at"
    assert catalog["campaign_roi"].dimensions == ("campaign", "channel")
    assert catalog["campaign_roi"].unit == "ratio"
    assert {
        metric_id: (metric.time_field, metric.unit, metric.dimensions)
        for metric_id, metric in catalog.items()
    } == EXPECTED_METADATA
    for metric in catalog.values():
        assert metric.version == "1.0.0"
        assert metric.valid_from.isoformat() == "2025-01-01T00:00:00+00:00"
        assert metric.source_tables
        assert metric.dimensions


def test_core_catalog_examples_are_required_envelope_metadata() -> None:
    raw = yaml.safe_load(Path("data/metrics/core.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert all(
        isinstance(item["example_question"], str) and item["example_question"].strip()
        for item in raw
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("source_tables", "[unknown_table]", "unknown source table"),
        ("valid_from", "2025-01-01T00:00:00", "UTC-aware"),
        ("expression_sql", "sum(", "invalid expression_sql"),
        ("expression_sql", "1; delete from orders", "single SELECT"),
    ],
)
def test_loader_rejects_unsafe_or_invalid_metadata(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    catalog_path = tmp_path / "metrics.yaml"
    catalog_path.write_text(
        "- metric_id: gmv\n"
        "  name_zh: 成交额\n"
        "  name_en: GMV\n"
        "  description: 有效订单商品净额\n"
        "  expression_sql: \"sum(oi.net_amount)\"\n"
        "  time_field: o.ordered_at\n"
        "  default_filters: [\"o.status in ('paid', 'completed', 'refunded')\"]\n"
        "  dimensions: [region]\n"
        "  source_tables: [orders, order_items]\n"
        "  unit: cny\n"
        "  version: 1.0.0\n"
        "  valid_from: 2025-01-01T00:00:00Z\n"
        "  example_question: 这个月成交额是多少?\n",
        encoding="utf-8",
    )
    content = catalog_path.read_text(encoding="utf-8")
    replacement = (
        f"  source_tables: {value}"
        if field == "source_tables"
        else f"  valid_from: {value}"
        if field == "valid_from"
        else f"  expression_sql: \"{value}\""
    )
    catalog_path.write_text(
        content.replace(
            "  source_tables: [orders, order_items]"
            if field == "source_tables"
            else "  valid_from: 2025-01-01T00:00:00Z"
            if field == "valid_from"
            else "  expression_sql: \"sum(oi.net_amount)\"",
            replacement,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=match):
        load_metric_catalog(catalog_path)


def test_loader_rejects_duplicate_ids_and_catalog_envelope_errors(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    core_text = Path("data/metrics/core.yaml").read_text(encoding="utf-8")
    core_first_entry = core_text.split("- metric_id:", 2)[1]
    path.write_text(
        f"- metric_id:{core_first_entry}- metric_id:{core_first_entry}", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="duplicate metric_id"):
        load_metric_catalog(path)


def test_get_metric_returns_catalog_metric_and_explains_unknown_id() -> None:
    assert get_metric("gmv").metric_id == "gmv"
    with pytest.raises(KeyError, match="unknown metric_id: not_a_metric"):
        get_metric("not_a_metric")
