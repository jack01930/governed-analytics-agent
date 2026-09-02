# ruff: noqa: E501

from pathlib import Path

import pytest
import sqlglot
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

TRUSTED_COMPLEX_EXPRESSIONS = {
    "net_revenue": """(with order_payments as (select p.order_id, sum(p.amount) as amount from payments p join orders o on o.order_id = p.order_id where o.ordered_at >= :start_at and o.ordered_at < :end_at and p.status = 'succeeded' group by p.order_id), order_refunds as (select r.order_id, sum(r.amount) as amount from refunds r join orders o on o.order_id = r.order_id where o.ordered_at >= :start_at and o.ordered_at < :end_at and r.status = 'succeeded' group by r.order_id) select coalesce((select sum(amount) from order_payments), 0) - coalesce((select sum(amount) from order_refunds), 0))""",
    "refund_rate": """(with order_payments as (select p.order_id, sum(p.amount) as amount from payments p join orders o on o.order_id = p.order_id where o.ordered_at >= :start_at and o.ordered_at < :end_at and p.status = 'succeeded' group by p.order_id), order_refunds as (select r.order_id, sum(r.amount) as amount from refunds r join orders o on o.order_id = r.order_id where o.ordered_at >= :start_at and o.ordered_at < :end_at and r.status = 'succeeded' group by r.order_id) select coalesce((select sum(amount) from order_refunds), 0) / nullif((select sum(amount) from order_payments), 0))""",
    "repeat_purchase_rate": """(with active_customers as (select distinct o.customer_id from orders o where o.ordered_at >= :start_at and o.ordered_at < :end_at and o.status in ('paid', 'completed', 'refunded')), lifetime_orders as (select o.customer_id from orders o where o.ordered_at < :end_at and o.status in ('paid', 'completed', 'refunded') group by o.customer_id having count(distinct o.order_id) >= 2) select count(lo.customer_id)::numeric / nullif(count(ac.customer_id), 0) from active_customers ac left join lifetime_orders lo on lo.customer_id = ac.customer_id)""",
    "customer_acquisition_cost": """(with interval_attributions as (select distinct a.campaign_id, o.customer_id from campaign_attributions a join orders o on o.order_id = a.order_id where a.attributed_at >= :start_at and a.attributed_at < :end_at), campaign_spend as (select mc.campaign_id, mc.spend from marketing_campaigns mc join (select distinct campaign_id from interval_attributions) ia on ia.campaign_id = mc.campaign_id) select coalesce((select sum(spend) from campaign_spend), 0) / nullif((select count(distinct customer_id) from interval_attributions), 0))""",
    "campaign_roi": """(with interval_revenue as (select a.campaign_id, sum(a.attributed_revenue) as amount from campaign_attributions a where a.attributed_at >= :start_at and a.attributed_at < :end_at group by a.campaign_id), campaign_spend as (select mc.campaign_id, mc.spend from marketing_campaigns mc join interval_revenue ir on ir.campaign_id = mc.campaign_id) select (coalesce((select sum(amount) from interval_revenue), 0) - coalesce((select sum(spend) from campaign_spend), 0)) / nullif((select sum(spend) from campaign_spend), 0))""",
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


def test_complex_catalog_expressions_match_trusted_scalar_contracts() -> None:
    catalog = load_metric_catalog("data/metrics/core.yaml")
    for metric_id, trusted_expression in TRUSTED_COMPLEX_EXPRESSIONS.items():
        expected = sqlglot.parse_one(f"select {trusted_expression}", read="postgres")
        actual = sqlglot.parse_one(f"select {catalog[metric_id].expression_sql}", read="postgres")
        assert actual == expected


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("source_tables", "[unknown_table]", "unknown source table"),
        ("valid_from", "2025-01-01T00:00:00", "UTC-aware"),
        ("expression_sql", "sum(", "invalid expression_sql"),
        ("expression_sql", "1; delete from orders", "single SELECT"),
        ("expression_sql", "1 into scratch", "single SELECT"),
        ("expression_sql", "1 /*;*/; select 2", "single SELECT"),
        ("expression_sql", "(with changed as (delete from orders returning order_id) select 1)", "single SELECT"),
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


def test_loader_rejects_duplicate_yaml_mapping_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-field.yaml"
    source = Path("data/metrics/core.yaml").read_text(encoding="utf-8")
    first_metric = source.split("- metric_id:", 2)[1]
    path.write_text(f"- metric_id:{first_metric}  unit: ratio\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate YAML key: unit"):
        load_metric_catalog(path)


def test_get_metric_returns_catalog_metric_and_explains_unknown_id() -> None:
    assert get_metric("gmv").metric_id == "gmv"
    with pytest.raises(KeyError, match="unknown metric_id: not_a_metric"):
        get_metric("not_a_metric")
