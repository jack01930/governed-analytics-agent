"""Static safety checks for the bounded PostgreSQL loader."""

from pathlib import Path

import pytest

from governed_analytics.data_generation.loader import (
    TABLE_LOAD_ORDER,
    TABLE_SPECS,
    load_csvs,
    table_spec,
)


def test_registry_is_complete_ordered_and_excludes_generated_identities() -> None:
    assert TABLE_LOAD_ORDER == (
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
    assert tuple(TABLE_SPECS) == TABLE_LOAD_ORDER
    for name in TABLE_LOAD_ORDER:
        spec = TABLE_SPECS[name]
        assert spec.table_name == name
        assert spec.identity_column not in spec.columns
        assert spec.sort_by == (spec.identity_column,)


def test_registry_has_exact_non_generated_schema_columns() -> None:
    expected = {
        "categories": ("category_id", ("category_code", "category_name", "created_at")),
        "customers": ("customer_id", ("customer_code", "segment", "region", "registered_at")),
        "products": (
            "product_id",
            ("sku", "category_id", "product_name", "list_price", "unit_cost", "is_active"),
        ),
        "marketing_campaigns": (
            "campaign_id",
            ("campaign_code", "campaign_name", "channel", "start_at", "end_at", "spend"),
        ),
        "orders": (
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
        ),
        "order_items": (
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
        ),
        "payments": (
            "payment_id",
            ("payment_code", "order_id", "status", "provider", "amount", "paid_at", "created_at"),
        ),
        "refunds": (
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
        ),
        "inventory_snapshots": (
            "inventory_snapshot_id", ("snapshot_at", "product_id", "available_qty", "reserved_qty")
        ),
        "web_sessions": (
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
        ),
        "campaign_attributions": (
            "attribution_id", ("campaign_id", "order_id", "attributed_revenue", "attributed_at")
        ),
        "pipeline_runs": (
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
        ),
    }
    actual = {
        name: (spec.identity_column, spec.columns, spec.sort_by)
        for name, spec in TABLE_SPECS.items()
    }
    assert actual == {
        name: (identity, columns, (identity,)) for name, (identity, columns) in expected.items()
    }


def test_unknown_table_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown registry table"):
        table_spec("pg_catalog.pg_authid")
    with pytest.raises(ValueError, match="unknown"):
        load_csvs({"pg_catalog.pg_authid": Path(__file__)})
