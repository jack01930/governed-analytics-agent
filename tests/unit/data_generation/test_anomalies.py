"""Contracts for deterministic anomaly injection and its truth manifest."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation.anomalies import (
    AnomalyManifest,
    anomaly_count_plan,
    inject_anomalies,
)
from governed_analytics.data_generation.dimensions import generate_products
from governed_analytics.data_generation.facts import generate_base_facts
from governed_analytics.data_generation.models import dataset_id_for_config, load_generator_config
from governed_analytics.data_generation.vocabulary import SOUTH_REGIONS


@pytest.fixture(scope="module")
def config():  # type: ignore[no-untyped-def]
    return load_generator_config("data/generator/tiny.yaml")


@pytest.fixture(scope="module")
def base(config):  # type: ignore[no-untyped-def]
    return generate_base_facts(config)


@pytest.fixture(scope="module")
def result(config, base):  # type: ignore[no-untyped-def]
    return inject_anomalies(config, base)


def _window(frame: pd.DataFrame, column: str, start: datetime, end: datetime) -> pd.DataFrame:
    return frame[frame[column].between(start, end, inclusive="left")]


def test_injection_does_not_mutate_base_and_is_repeatable(config, base, result) -> None:  # type: ignore[no-untyped-def]
    """Each run copies all frames and produces byte-stable Pydantic truth."""
    fresh = generate_base_facts(config)
    again = inject_anomalies(config, base)
    for name in (
        "orders",
        "order_items",
        "payments",
        "refunds",
        "web_sessions",
        "inventory_snapshots",
        "campaign_attributions",
        "pipeline_runs",
    ):
        assert getattr(base, name).equals(getattr(fresh, name))
        assert getattr(result, name).equals(getattr(again, name))
    assert result.manifest.model_dump_json() == again.manifest.model_dump_json()


def test_manifest_has_stable_dataset_id_order_schema_and_lookup(config, result) -> None:  # type: ignore[no-untyped-def]
    expected_ids = (
        "anomaly_gmv_drop_south_conversion",
        "anomaly_gmv_drop_stockout",
        "anomaly_refund_spike_category",
        "anomaly_inventory_delay",
        "anomaly_duplicate_order_items",
        "anomaly_order_amount_mismatch",
        "anomaly_missing_region",
        "anomaly_refund_exceeds_payment",
    )
    assert result.manifest.dataset_id == dataset_id_for_config(config)
    assert tuple(record.anomaly_id for record in result.manifest.anomalies) == expected_ids
    assert len(set(expected_ids)) == 8
    assert all(
        record.start_at.tzinfo is not None and record.end_at.tzinfo is not None
        for record in result.manifest.anomalies
    )
    assert all(record.start_at < record.end_at for record in result.manifest.anomalies)
    assert result.manifest.by_id(expected_ids[0]).anomaly_id == expected_ids[0]
    with pytest.raises(KeyError, match="unknown anomaly_id"):
        result.manifest.by_id("does-not-exist")

    committed = Path("data/manifests/anomaly_manifest.schema.json").read_text(encoding="utf-8")
    generated = (
        json.dumps(
            AnomalyManifest.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )
    assert committed == generated


def test_quality_anomalies_have_exact_tiny_counts_and_stable_selection(base, result) -> None:  # type: ignore[no-untyped-def]
    duplicate_day = datetime(2026, 4, 10, tzinfo=UTC)
    mismatch_day = datetime(2026, 3, 17, tzinfo=UTC)
    missing_day = datetime(2026, 2, 12, tzinfo=UTC)
    refund_day = datetime(2026, 5, 20, tzinfo=UTC)

    assert result.order_items["source_line_id"].duplicated(keep=False).sum() == 40
    duplicated = result.order_items[result.order_items["source_line_id"].duplicated(keep=False)]
    source_ids = duplicated["source_line_id"].drop_duplicates().tolist()
    expected_sources = (
        _window(
            base.order_items.merge(base.orders[["order_id", "ordered_at"]], on="order_id"),
            "ordered_at",
            duplicate_day,
            duplicate_day + pd.Timedelta(days=1),
        )
        .sort_values("order_item_id")["source_line_id"]
        .head(20)
        .tolist()
    )
    assert source_ids == expected_sources
    assert result.manifest.by_id("anomaly_duplicate_order_items").mutated_rows == 20

    mismatches = (
        result.orders.set_index("order_id")["payable_amount"]
        - base.orders.set_index("order_id")["payable_amount"]
    )
    changed_mismatch = mismatches[mismatches == Decimal("10.00")].index.tolist()
    expected_mismatch = (
        _window(base.orders, "ordered_at", mismatch_day, mismatch_day + pd.Timedelta(days=1))
        .sort_values("order_id")
        .head(10)["order_id"]
        .tolist()
    )
    assert changed_mismatch == expected_mismatch
    assert result.manifest.by_id("anomaly_order_amount_mismatch").mutated_rows == 10

    assert result.orders["region"].isna().sum() == 10
    expected_missing = (
        _window(base.orders, "ordered_at", missing_day, missing_day + pd.Timedelta(days=1))
        .sort_values("order_id")
        .head(10)["order_id"]
        .tolist()
    )
    assert (
        result.orders.loc[result.orders["region"].isna(), "order_id"].tolist() == expected_missing
    )
    assert result.manifest.by_id("anomaly_missing_region").mutated_rows == 10

    payments = result.payments.set_index("order_id")
    refund_violations = result.refunds.merge(
        payments[["amount"]], left_on="order_id", right_index=True
    )
    violating = refund_violations[refund_violations["amount_x"] > refund_violations["amount_y"]]
    expected_refunds = (
        _window(base.refunds, "refunded_at", refund_day, refund_day + pd.Timedelta(days=1))
        .sort_values("refund_id")
        .head(3)["refund_id"]
        .tolist()
    )
    assert violating["refund_id"].tolist() == expected_refunds
    assert len(violating) == 3
    assert result.manifest.by_id("anomaly_refund_exceeds_payment").mutated_rows == 3


def test_conversion_stockout_and_inventory_mutations_reconcile(base, result) -> None:  # type: ignore[no-untyped-def]
    start = datetime(2026, 6, 8, tzinfo=UTC)
    end = datetime(2026, 6, 15, tzinfo=UTC)
    base_orders = base.orders.set_index("order_id")
    base_sessions = base.web_sessions.merge(
        base_orders[["region"]], left_on="order_id", right_index=True, how="left"
    )
    candidates = _window(
        base_sessions[base_sessions["converted"] & base_sessions["region"].isin(SOUTH_REGIONS)],
        "occurred_at",
        start,
        end,
    ).sort_values("session_id")
    expected_flipped = candidates.head(int(len(candidates) * 0.35))["session_id"].tolist()
    final_sessions = result.web_sessions.set_index("session_id")
    assert final_sessions.loc[expected_flipped, "converted"].eq(False).all()
    assert final_sessions.loc[expected_flipped, "order_id"].isna().all()
    assert result.manifest.by_id("anomaly_gmv_drop_south_conversion").mutated_rows == len(
        expected_flipped
    )

    affected_items = _window(
        base.order_items.merge(base_orders[["ordered_at"]], on="order_id"), "ordered_at", start, end
    )
    target_items = affected_items[affected_items["product_id"].isin((1, 2))].sort_values(
        "order_item_id"
    )
    removed = target_items.head(int(len(target_items) * 0.90))["order_item_id"].tolist()
    surviving_source_ids = set(result.order_items["source_line_id"])
    assert (
        not set(base.order_items.set_index("order_item_id").loc[removed, "source_line_id"])
        & surviving_source_ids
    )
    assert result.manifest.by_id("anomaly_gmv_drop_stockout").mutated_rows == len(removed)
    final_orders = result.orders.set_index("order_id")
    final_payments = result.payments.set_index("order_id")
    final_item_totals = result.order_items.groupby("order_id")[
        ["gross_amount", "discount_amount", "net_amount"]
    ].sum()
    affected_order_ids = set(target_items.head(int(len(target_items) * 0.90))["order_id"])
    for order_id in affected_order_ids:
        if order_id not in final_item_totals.index:
            assert final_orders.loc[order_id, "status"] == "cancelled"
            assert (
                final_orders.loc[
                    order_id,
                    ["gross_amount", "discount_amount", "shipping_amount", "payable_amount"],
                ]
                .eq(Decimal("0.00"))
                .all()
            )
            assert final_payments.loc[order_id, "status"] == "failed"
            assert final_payments.loc[order_id, "amount"] == Decimal("0.00")
            assert pd.isna(final_payments.loc[order_id, "paid_at"])
        else:
            totals = final_item_totals.loc[order_id]
            assert final_orders.loc[order_id, "gross_amount"] == totals["gross_amount"]
            assert final_orders.loc[order_id, "discount_amount"] == totals["discount_amount"]
            assert final_orders.loc[order_id, "payable_amount"] == (
                totals["net_amount"] + final_orders.loc[order_id, "shipping_amount"]
            )
            if final_payments.loc[order_id, "status"] == "succeeded":
                assert (
                    final_payments.loc[order_id, "amount"]
                    == final_orders.loc[order_id, "payable_amount"]
                )
    snapshot_day = result.inventory_snapshots[
        result.inventory_snapshots["snapshot_at"].eq(datetime(2026, 6, 8, 12, tzinfo=UTC))
    ]
    assert snapshot_day.set_index("product_id").loc[[1, 2], "available_qty"].eq(0).all()

    stale = result.inventory_snapshots[
        result.inventory_snapshots["snapshot_at"].dt.date.eq(
            datetime(2026, 6, 15, tzinfo=UTC).date()
        )
    ]
    assert stale.empty
    pipeline = result.pipeline_runs[
        (result.pipeline_runs["pipeline_name"] == "inventory")
        & (result.pipeline_runs["started_at"].dt.date == datetime(2026, 6, 15, tzinfo=UTC).date())
    ].iloc[0]
    assert pipeline.status == "failed" and pipeline.error_code is not None
    assert pipeline.watermark == datetime(2026, 6, 15, tzinfo=UTC)


def test_refund_spike_and_final_relational_contract(config, base, result) -> None:  # type: ignore[no-untyped-def]
    start = datetime(2026, 5, 4, tzinfo=UTC)
    end = datetime(2026, 5, 11, tzinfo=UTC)
    products = generate_products(config).set_index("product_id")
    paid_orders = set(base.payments.loc[base.payments["status"] == "succeeded", "order_id"])
    eligible_items = base.order_items[
        base.order_items["product_id"].map(products["category_id"]).eq(18)
    ]
    eligible_orders = base.orders.merge(
        eligible_items[["order_id"]].drop_duplicates(), on="order_id"
    )
    eligible_orders = _window(eligible_orders, "ordered_at", start, end)
    eligible_orders = eligible_orders[eligible_orders["order_id"].isin(paid_orders)].sort_values(
        "order_id"
    )
    expected_coverage = int(len(eligible_orders) * 0.24)
    successful = result.refunds[result.refunds["status"] == "succeeded"]
    coverage = successful[successful["order_id"].isin(eligible_orders["order_id"])][
        "order_id"
    ].nunique()
    assert coverage == expected_coverage
    added = len(result.refunds) - len(base.refunds)
    assert result.manifest.by_id("anomaly_refund_spike_category").mutated_rows == added
    added_rows = result.refunds.iloc[-added:] if added else result.refunds.iloc[0:0]
    payments = result.payments.set_index("order_id")
    final_items = result.order_items.set_index("order_item_id")
    assert (added_rows["amount"] > Decimal("0.00")).all()
    assert (
        added_rows["amount"].to_numpy() <= payments.loc[added_rows["order_id"], "amount"].to_numpy()
    ).all()
    assert (
        added_rows["amount"].to_numpy()
        <= final_items.loc[added_rows["order_item_id"], "net_amount"].to_numpy()
    ).all()
    assert added_rows["refunded_at"].between(start, end, inclusive="left").all()

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
    for frame_name, identity in identities.items():
        frame = getattr(result, frame_name)
        assert frame[identity].tolist() == list(range(1, len(frame) + 1))
    assert str(result.refunds["order_item_id"].dtype) == "Int64"
    assert set(result.order_items["order_id"]) <= set(result.orders["order_id"])
    assert set(result.refunds["order_id"]) <= set(result.orders["order_id"])
    assert set(result.refunds.dropna(subset=["order_item_id"])["order_item_id"]) <= set(
        result.order_items["order_item_id"]
    )
    assert (
        result.manifest.by_id("anomaly_inventory_delay").mutation["pipeline_action"]
        == "status_changed"
    )


def test_count_plan_locks_full_without_generating_full() -> None:
    assert anomaly_count_plan("tiny") == {
        "duplicate_order_items": 20,
        "order_amount_mismatch": 10,
        "missing_region": 10,
        "refund_exceeds_payment": 3,
    }
    assert anomaly_count_plan("full") == {
        "duplicate_order_items": 2000,
        "order_amount_mismatch": 1000,
        "missing_region": 1000,
        "refund_exceeds_payment": 300,
    }
