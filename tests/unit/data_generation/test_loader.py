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
    assert TABLE_SPECS["categories"].columns == (
        "category_code",
        "category_name",
        "created_at",
    )
    assert TABLE_SPECS["orders"].columns == (
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
    )
    assert TABLE_SPECS["pipeline_runs"].columns == (
        "pipeline_name",
        "started_at",
        "finished_at",
        "status",
        "watermark",
        "row_count",
        "error_code",
    )


def test_unknown_table_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown registry table"):
        table_spec("pg_catalog.pg_authid")
    with pytest.raises(ValueError, match="unknown"):
        load_csvs({"pg_catalog.pg_authid": Path(__file__)})
