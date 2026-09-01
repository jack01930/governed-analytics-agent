"""Contracts for clean, deterministic ecommerce fact generation."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation.dimensions import generate_customers, generate_products
from governed_analytics.data_generation.facts import daily_order_weights, generate_base_facts
from governed_analytics.data_generation.models import load_generator_config
from governed_analytics.data_generation.vocabulary import CHANNELS, REGIONS, SOUTH_REGIONS


@pytest.fixture(scope="module")
def config():  # type: ignore[no-untyped-def]
    """Load the small deterministic generation configuration once per module."""
    return load_generator_config("data/generator/tiny.yaml")


@pytest.fixture(scope="module")
def facts(config):  # type: ignore[no-untyped-def]
    """Avoid regenerating the same tiny dataset for each independent assertion."""
    return generate_base_facts(config)


def test_base_facts_are_referentially_valid_and_repeatable(config, facts) -> None:  # type: ignore[no-untyped-def]
    """The baseline fact bundle has the configured order and session counts."""
    second = generate_base_facts(config)
    assert len(facts.orders) == 3000
    assert len(facts.web_sessions) == 15000
    assert facts.orders.equals(second.orders)
    assert set(facts.order_items["order_id"]) <= set(facts.orders["order_id"])
    assert set(facts.payments["order_id"]) == set(facts.orders["order_id"])
    assert facts.orders["ordered_at"].min() >= config.start_at
    assert facts.orders["ordered_at"].max() < config.end_at


def test_fact_frames_match_schema_order_counts_and_identity_order(facts) -> None:  # type: ignore[no-untyped-def]
    """All clean facts retain database column order and contiguous identities."""
    expected_columns = {
        "orders": [
            "order_id",
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
        ],
        "order_items": [
            "order_item_id",
            "source_line_id",
            "order_id",
            "product_id",
            "quantity",
            "unit_price",
            "discount_amount",
            "gross_amount",
            "net_amount",
        ],
        "payments": [
            "payment_id",
            "payment_code",
            "order_id",
            "status",
            "provider",
            "amount",
            "paid_at",
            "created_at",
        ],
        "refunds": [
            "refund_id",
            "refund_code",
            "order_id",
            "order_item_id",
            "status",
            "amount",
            "reason",
            "refunded_at",
            "created_at",
        ],
        "web_sessions": [
            "session_id",
            "session_code",
            "customer_id",
            "order_id",
            "channel",
            "occurred_at",
            "converted",
            "duration_seconds",
        ],
        "inventory_snapshots": [
            "inventory_snapshot_id",
            "snapshot_at",
            "product_id",
            "available_qty",
            "reserved_qty",
        ],
        "campaign_attributions": [
            "attribution_id",
            "campaign_id",
            "order_id",
            "attributed_revenue",
            "attributed_at",
        ],
        "pipeline_runs": [
            "pipeline_run_id",
            "pipeline_name",
            "started_at",
            "finished_at",
            "status",
            "watermark",
            "row_count",
            "error_code",
        ],
    }
    expected_counts = {
        "orders": 3000,
        "payments": 3000,
        "web_sessions": 15000,
        "inventory_snapshots": 54600,
        "pipeline_runs": 1638,
    }
    identities = {
        "orders": "order_id",
        "order_items": "order_item_id",
        "payments": "payment_id",
        "refunds": "refund_id",
        "web_sessions": "session_id",
        "inventory_snapshots": "inventory_snapshot_id",
        "campaign_attributions": "attribution_id",
        "pipeline_runs": "pipeline_run_id",
    }

    for name, columns in expected_columns.items():
        frame = getattr(facts, name)
        assert list(frame.columns) == columns
        identity = identities[name]
        assert frame[identity].tolist() == list(range(1, len(frame) + 1))
    for name, count in expected_counts.items():
        assert len(getattr(facts, name)) == count


def test_fact_foreign_keys_vocabulary_and_timestamps_are_valid(config, facts) -> None:  # type: ignore[no-untyped-def]
    """Every FK, code and clean-table time window is valid before anomaly injection."""
    orders = facts.orders
    assert set(facts.order_items["order_id"]) <= set(orders["order_id"])
    assert set(facts.order_items["product_id"]) <= set(range(1, config.products + 1))
    assert set(facts.payments["order_id"]) == set(orders["order_id"])
    assert set(facts.refunds["order_id"]) <= set(orders["order_id"])
    assert set(facts.refunds["order_item_id"]) <= set(facts.order_items["order_item_id"])
    assert set(facts.web_sessions.dropna(subset=["customer_id"])["customer_id"]) <= set(
        range(1, config.customers + 1)
    )
    assert set(facts.web_sessions.dropna(subset=["order_id"])["order_id"]) <= set(
        orders["order_id"]
    )
    assert set(facts.campaign_attributions["campaign_id"]) <= set(range(1, config.campaigns + 1))
    assert set(facts.campaign_attributions["order_id"]) <= set(orders["order_id"])
    assert orders["order_code"].is_unique
    assert facts.order_items["source_line_id"].is_unique
    assert facts.payments["payment_code"].is_unique
    assert facts.refunds["refund_code"].is_unique
    assert facts.web_sessions["session_code"].is_unique
    assert set(orders["region"]) <= set(REGIONS)
    assert set(orders["channel"]) <= set(CHANNELS)
    assert (orders["currency"] == "CNY").all()
    assert orders["ordered_at"].between(config.start_at, config.end_at, inclusive="left").all()
    registrations = generate_customers(config).set_index("customer_id")["registered_at"]
    assert (
        registrations.loc[orders["customer_id"]].to_numpy() <= orders["ordered_at"].to_numpy()
    ).all()
    assert (
        facts.web_sessions["occurred_at"]
        .between(config.start_at, config.end_at, inclusive="left")
        .all()
    )


def test_money_payment_refund_and_status_invariants_are_exact(facts) -> None:  # type: ignore[no-untyped-def]
    """Base facts have exact Decimal money and no undeclared quality inconsistency."""
    orders = facts.orders.set_index("order_id")
    items = facts.order_items
    payments = facts.payments.set_index("order_id")
    refunds = facts.refunds

    values = list(orders["gross_amount"]) + list(items["unit_price"]) + list(payments["amount"])
    assert all(type(value) is Decimal and value.as_tuple().exponent == -2 for value in values)
    assert all(value >= Decimal("0.00") for value in values)
    assert (items["gross_amount"] == items["unit_price"] * items["quantity"]).all()
    assert (items["net_amount"] == items["gross_amount"] - items["discount_amount"]).all()
    assert items["quantity"].between(1, 3).all()
    assert items.groupby("order_id").size().between(1, 5).all()
    assert (items["discount_amount"] <= items["gross_amount"]).all()
    totals = items.groupby("order_id")[["gross_amount", "discount_amount", "net_amount"]].sum()
    for order_id, row in totals.iterrows():
        assert orders.loc[order_id, "gross_amount"] == row["gross_amount"]
        assert orders.loc[order_id, "discount_amount"] == row["discount_amount"]
        assert (
            orders.loc[order_id, "payable_amount"]
            == row["net_amount"] + orders.loc[order_id, "shipping_amount"]
        )

    succeeded = payments["status"] == "succeeded"
    valid_statuses = {"paid", "completed", "refunded"}
    assert succeeded[orders["status"].isin(valid_statuses)].all()
    assert (~succeeded[~orders["status"].isin(valid_statuses)]).all()
    assert (payments.loc[succeeded, "amount"] == orders.loc[succeeded, "payable_amount"]).all()
    assert payments.loc[succeeded, "paid_at"].notna().all()
    assert (payments.loc[succeeded, "paid_at"] >= orders.loc[succeeded, "ordered_at"]).all()
    assert set(orders.loc[~succeeded, "status"]) <= {"placed", "cancelled"}
    assert refunds["status"].eq("succeeded").all()
    refund_items = items.set_index("order_item_id").loc[refunds["order_item_id"]]
    refund_payments = payments.loc[refunds["order_id"]]
    assert refund_payments["status"].eq("succeeded").all()
    assert (refunds["amount"].to_numpy() <= refund_items["net_amount"].to_numpy()).all()
    assert (refunds["amount"].to_numpy() <= refund_payments["amount"].to_numpy()).all()
    assert (refunds["refunded_at"].to_numpy() >= refund_payments["paid_at"].to_numpy()).all()
    assert refunds["refunded_at"].notna().all()


def test_sessions_inventory_pipeline_and_attribution_contracts(config, facts) -> None:  # type: ignore[no-untyped-def]
    """Event, inventory, attribution and freshness facts form complete clean baselines."""
    orders = facts.orders.set_index("order_id")
    sessions = facts.web_sessions
    converted = sessions[sessions["converted"]]
    unconverted = sessions[~sessions["converted"]]
    assert len(converted) == len(orders)
    assert converted["order_id"].is_unique
    assert set(converted["order_id"]) == set(orders.index)
    assert unconverted["order_id"].isna().all()
    assert (
        converted["customer_id"].to_numpy()
        == orders.loc[converted["order_id"], "customer_id"].to_numpy()
    ).all()
    assert (
        converted["channel"].to_numpy() == orders.loc[converted["order_id"], "channel"].to_numpy()
    ).all()
    assert (
        converted["occurred_at"].to_numpy()
        <= orders.loc[converted["order_id"], "ordered_at"].to_numpy()
    ).all()
    assert facts.inventory_snapshots.duplicated(["snapshot_at", "product_id"]).sum() == 0
    assert facts.inventory_snapshots.groupby("product_id").size().eq(546).all()
    assert facts.inventory_snapshots["snapshot_at"].dt.hour.eq(12).all()
    assert (facts.inventory_snapshots[["available_qty", "reserved_qty"]] >= 0).all().all()
    assert facts.pipeline_runs.groupby("pipeline_name").size().to_dict() == {
        "inventory": 546,
        "orders": 546,
        "sessions": 546,
    }
    assert facts.pipeline_runs["status"].eq("succeeded").all()
    assert (facts.pipeline_runs["finished_at"] >= facts.pipeline_runs["started_at"]).all()
    assert (
        facts.pipeline_runs["watermark"]
        .between(config.start_at, config.end_at, inclusive="right")
        .all()
    )

    campaign_windows = pd.DataFrame(
        {
            "campaign_id": range(1, config.campaigns + 1),
            "start_at": [
                config.start_at + pd.Timedelta(days=14 * index) for index in range(config.campaigns)
            ],
            "end_at": [
                config.start_at + pd.Timedelta(days=14 * (index + 1))
                for index in range(config.campaigns)
            ],
        }
    ).set_index("campaign_id")
    assert facts.campaign_attributions.duplicated(["campaign_id", "order_id"]).sum() == 0
    for row in facts.campaign_attributions.itertuples(index=False):
        window = campaign_windows.loc[row.campaign_id]
        order = orders.loc[row.order_id]
        assert window["start_at"] <= order["ordered_at"] < window["end_at"]
        assert row.attributed_revenue <= order["payable_amount"]


def test_anomaly_anchor_candidates_are_clean_and_exact_at_tiny_scale(config, facts) -> None:  # type: ignore[no-untyped-def]
    """Stable ascending-PK Task 4 filters have all required legal baseline candidates."""
    orders = facts.orders
    items = facts.order_items
    payments = facts.payments
    refunds = facts.refunds
    order_dates = orders["ordered_at"].dt.date
    item_dates = items.merge(orders[["order_id", "ordered_at"]], on="order_id")[
        "ordered_at"
    ].dt.date
    product_categories = generate_products(config).set_index("product_id")["category_id"]

    assert (item_dates == date(2026, 4, 10)).sum() >= 20
    assert (order_dates == date(2026, 3, 17)).sum() >= 10
    assert (order_dates == date(2026, 2, 12)).sum() >= 10
    assert (refunds["refunded_at"].dt.date == date(2026, 5, 20)).sum() >= 3
    refund_order_ids = set(
        refunds.loc[refunds["refunded_at"].dt.date == date(2026, 5, 20), "order_id"]
    )
    assert (
        payments.set_index("order_id").loc[list(refund_order_ids), "status"].eq("succeeded").all()
    )
    june_window = orders["ordered_at"].between(
        datetime(2026, 6, 8, tzinfo=UTC), datetime(2026, 6, 16, tzinfo=UTC), inclusive="left"
    )
    south_june_orders = orders.loc[june_window & orders["region"].isin(SOUTH_REGIONS)]
    assert len(south_june_orders) >= 40
    june_items = items.merge(orders[["order_id", "ordered_at"]], on="order_id")
    assert (
        june_items["ordered_at"].between(
            datetime(2026, 6, 8, tzinfo=UTC), datetime(2026, 6, 16, tzinfo=UTC), inclusive="left"
        )
        & june_items["product_id"].isin({1, 2})
    ).sum() >= 40
    cat18_items = items[items["product_id"].map(product_categories).eq(18)].merge(
        orders[["order_id", "ordered_at"]], on="order_id"
    )
    assert (
        cat18_items["ordered_at"]
        .between(
            datetime(2026, 5, 4, tzinfo=UTC), datetime(2026, 5, 12, tzinfo=UTC), inclusive="left"
        )
        .sum()
        >= 25
    )


def test_daily_order_weights_encode_the_business_seasonality(config) -> None:  # type: ignore[no-untyped-def]
    """Seasonal weight math is deterministic and independently inspectable."""
    weights = daily_order_weights(config.start_at, config.end_at)
    assert weights.sum() == pytest.approx(1.0)
    by_day = dict(
        zip(pd.date_range(config.start_at, config.end_at, inclusive="left"), weights, strict=True)
    )
    assert (
        by_day[pd.Timestamp("2025-12-05", tz="UTC")] > by_day[pd.Timestamp("2025-11-05", tz="UTC")]
    )
    assert (
        by_day[pd.Timestamp("2025-12-05", tz="UTC")] > by_day[pd.Timestamp("2025-12-04", tz="UTC")]
    )
    assert (
        by_day[pd.Timestamp("2026-06-05", tz="UTC")] < by_day[pd.Timestamp("2026-05-01", tz="UTC")]
    )
