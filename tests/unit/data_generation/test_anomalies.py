"""Contracts for deterministic anomaly injection and its truth manifest."""

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation.anomalies import (
    AnomalyManifest,
    AnomalyRecord,
    anomaly_count_plan,
    inject_anomalies,
    selected_keys_sha256,
    stockout_count_plan,
)
from governed_analytics.data_generation.dimensions import generate_products
from governed_analytics.data_generation.facts import GeneratedFacts, generate_base_facts
from governed_analytics.data_generation.models import (
    GeneratorConfig,
    dataset_id_for_config,
    load_generator_config,
)
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


def test_manifest_and_record_models_reject_invalid_truth_boundaries(result) -> None:  # type: ignore[no-untyped-def]
    record = result.manifest.anomalies[0]
    valid_dataset_id = result.manifest.dataset_id
    record_data = record.model_dump()
    with pytest.raises(ValueError, match="UTC-aware"):
        AnomalyRecord.model_validate({**record_data, "start_at": datetime(2026, 6, 8)})
    with pytest.raises(ValueError, match="UTC-aware"):
        AnomalyRecord.model_validate(
            {
                **record_data,
                "start_at": datetime(2026, 6, 8, tzinfo=timezone(timedelta(hours=8))),
            }
        )
    with pytest.raises(ValueError, match="start_at must be before end_at"):
        AnomalyRecord.model_validate(
            {**record_data, "start_at": record.end_at, "end_at": record.start_at}
        )
    with pytest.raises(ValueError, match="dataset_id"):
        AnomalyManifest(dataset_id="not-a-digest", anomalies=result.manifest.anomalies)
    with pytest.raises(ValueError, match="exactly match"):
        AnomalyManifest(dataset_id=valid_dataset_id, anomalies=())
    with pytest.raises(ValueError, match="exactly match"):
        AnomalyManifest(
            dataset_id=valid_dataset_id,
            anomalies=(*result.manifest.anomalies[:-1], record),
        )
    with pytest.raises(ValueError, match="exactly match"):
        AnomalyManifest(
            dataset_id=valid_dataset_id,
            anomalies=(result.manifest.anomalies[1], record, *result.manifest.anomalies[2:]),
        )
    assert AnomalyManifest(dataset_id=valid_dataset_id, anomalies=result.manifest.anomalies)
    with pytest.raises(ValueError, match="extra_forbidden"):
        AnomalyManifest.model_validate({**result.manifest.model_dump(), "unexpected": True})
    with pytest.raises(ValueError, match="extra_forbidden"):
        AnomalyRecord.model_validate({**record_data, "unexpected": True})


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
    target_items = affected_items[affected_items["product_id"].isin((1, 2))]
    removed: list[int] = []
    for product_id in (1, 2):
        sku_scope = target_items[target_items["product_id"].eq(product_id)].sort_values(
            "order_item_id"
        )
        expected_removed = sku_scope.head(int(len(sku_scope) * 0.90))["order_item_id"].tolist()
        removed.extend(expected_removed)
        remaining_source_ids = set(result.order_items["source_line_id"])
        assert len(expected_removed) == 18
        assert (
            not set(
                base.order_items.set_index("order_item_id").loc[expected_removed, "source_line_id"]
            )
            & remaining_source_ids
        )
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
    affected_order_ids = set(base.order_items.set_index("order_item_id").loc[removed, "order_id"])
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
    freshness = result.manifest.by_id("anomaly_inventory_delay").expected_signals[0]
    assert freshness.operator == "stale"
    predicate, threshold = str(freshness.threshold).split("=", maxsplit=1)
    assert predicate == "watermark_lte"
    assert pipeline.watermark <= datetime.fromisoformat(threshold.replace("Z", "+00:00"))

    removed_refunds = base.refunds[base.refunds["order_item_id"].isin(removed)]
    stockout = result.manifest.by_id("anomaly_gmv_drop_stockout")
    assert stockout.mutation["linked_refund_rejected_count"] == len(removed_refunds)
    assert stockout.mutation["linked_refund_status_action"] == "set_rejected"
    assert stockout.mutation["linked_refund_item_fk_action"] == "clear"
    assert stockout.mutation["linked_refund_timestamp_action"] == "clear"
    final_refunds = result.refunds.set_index("refund_id")
    for refund_id in removed_refunds["refund_id"]:
        after = final_refunds.loc[refund_id]
        assert after["status"] == "rejected"
        assert pd.isna(after["order_item_id"])
        assert pd.isna(after["refunded_at"])


def test_stockout_rejects_refund_for_deleted_item_even_if_payment_still_succeeds(
    config: GeneratorConfig, base: GeneratedFacts
) -> None:
    """A nullable FK must not disguise a succeeded refund for an erased SKU line."""
    start = datetime(2026, 6, 8, tzinfo=UTC)
    end = datetime(2026, 6, 15, tzinfo=UTC)
    scoped = base.order_items.merge(base.orders[["order_id", "ordered_at"]], on="order_id")
    target = (
        scoped[
            scoped["product_id"].eq(1) & scoped["ordered_at"].between(start, end, inclusive="left")
        ]
        .sort_values("order_item_id")
        .iloc[0]
    )
    order_id = int(target["order_id"])
    item_id = int(target["order_item_id"])
    orders = base.orders.copy(deep=True)
    orders.loc[orders["order_id"].eq(order_id), ["region", "status"]] = ["北京", "paid"]
    payments = base.payments.copy(deep=True)
    paid_at = orders.loc[orders["order_id"].eq(order_id), "ordered_at"].iloc[0] + pd.Timedelta(
        minutes=1
    )
    payments.loc[payments["order_id"].eq(order_id), ["status", "paid_at"]] = [
        "succeeded",
        paid_at,
    ]
    items = base.order_items.copy(deep=True)
    extra = items.loc[items["order_item_id"].eq(item_id)].copy()
    extra["order_item_id"] = int(items["order_item_id"].max()) + 1
    extra["source_line_id"] = "LINE-REFUND-SURVIVOR"
    extra["product_id"] = 3
    items = pd.concat((items, extra), ignore_index=True)
    refunds = base.refunds.copy(deep=True)
    refunds = pd.concat(
        (
            refunds,
            pd.DataFrame(
                [
                    {
                        "refund_id": int(refunds["refund_id"].max()) + 1,
                        "refund_code": "REF-CONSTRUCTED",
                        "order_id": order_id,
                        "order_item_id": item_id,
                        "status": "succeeded",
                        "amount": Decimal("1.00"),
                        "reason": "构造性退款",
                        "refunded_at": paid_at + pd.Timedelta(minutes=1),
                        "created_at": paid_at + pd.Timedelta(minutes=1),
                    }
                ]
            ),
        ),
        ignore_index=True,
    )
    refunds["order_item_id"] = pd.Series(refunds["order_item_id"], dtype="Int64")
    constructed = GeneratedFacts(
        orders=orders,
        order_items=items,
        payments=payments,
        refunds=refunds,
        web_sessions=base.web_sessions,
        inventory_snapshots=base.inventory_snapshots,
        campaign_attributions=base.campaign_attributions,
        pipeline_runs=base.pipeline_runs,
    )
    injected = inject_anomalies(config, constructed)
    constructed_refund = injected.refunds.loc[injected.refunds["reason"].eq("构造性退款")].iloc[0]
    assert constructed_refund["status"] == "rejected"
    assert pd.isna(constructed_refund["order_item_id"])
    assert pd.isna(constructed_refund["refunded_at"])
    assert injected.payments.set_index("order_id").loc[order_id, "status"] == "succeeded"


def test_manifest_audit_keys_are_scoped_counted_and_digestible(base, result) -> None:  # type: ignore[no-untyped-def]
    for record in result.manifest.anomalies:
        assert any(item.startswith("scope=") for item in record.affected_keys)
        assert any(item.startswith("selected_count=") for item in record.affected_keys)
        digest = next(
            item for item in record.affected_keys if item.startswith("selected_keys_sha256=")
        )
        assert len(digest.removeprefix("selected_keys_sha256=")) == 64
        assert any(item.startswith("selection=") for item in record.affected_keys)
    start = datetime(2026, 6, 8, tzinfo=UTC)
    end = datetime(2026, 6, 15, tzinfo=UTC)
    orders = base.orders.set_index("order_id")
    candidates = base.web_sessions[
        base.web_sessions["converted"]
        & base.web_sessions["occurred_at"].between(start, end, inclusive="left")
        & base.web_sessions["order_id"].map(orders["region"]).isin(SOUTH_REGIONS)
    ].sort_values("session_id")
    selected_ids = candidates.head(int(len(candidates) * 0.35))["session_id"].tolist()
    conversion = result.manifest.by_id("anomaly_gmv_drop_south_conversion")
    assert f"selected_count={len(selected_ids)}" in conversion.affected_keys
    assert f"selected_keys_sha256={selected_keys_sha256(selected_ids)}" in conversion.affected_keys
    assert conversion.expected_signals[0].threshold != "south"
    assert result.manifest.by_id("anomaly_refund_spike_category").expected_signals[
        0
    ].threshold == Decimal("2")


def test_selected_key_digest_is_type_order_and_duplicate_sensitive() -> None:
    assert selected_keys_sha256([1]) != selected_keys_sha256(["1"])
    assert selected_keys_sha256([1, 2]) != selected_keys_sha256([2, 1])
    assert selected_keys_sha256([1]) != selected_keys_sha256([1, 1])
    with pytest.raises(TypeError, match="unsupported selected key type"):
        selected_keys_sha256([pd.NA])


def test_inventory_delay_uses_strict_complete_timestamp_cutoff(config, base) -> None:  # type: ignore[no-untyped-def]
    cutoff = datetime(2026, 6, 15, 8, tzinfo=UTC)
    snapshots = base.inventory_snapshots.copy(deep=True)
    boundary_rows = pd.DataFrame(
        [
            {
                "inventory_snapshot_id": int(snapshots["inventory_snapshot_id"].max()) + 1,
                "snapshot_at": cutoff,
                "product_id": 1,
                "available_qty": 10,
                "reserved_qty": 0,
            },
            {
                "inventory_snapshot_id": int(snapshots["inventory_snapshot_id"].max()) + 2,
                "snapshot_at": cutoff + timedelta(minutes=1),
                "product_id": 2,
                "available_qty": 10,
                "reserved_qty": 0,
            },
        ]
    )
    constructed = GeneratedFacts(
        orders=base.orders,
        order_items=base.order_items,
        payments=base.payments,
        refunds=base.refunds,
        web_sessions=base.web_sessions,
        inventory_snapshots=pd.concat((snapshots, boundary_rows), ignore_index=True),
        campaign_attributions=base.campaign_attributions,
        pipeline_runs=base.pipeline_runs,
    )
    injected = inject_anomalies(config, constructed)
    assert injected.inventory_snapshots["snapshot_at"].eq(cutoff).any()
    assert not injected.inventory_snapshots["snapshot_at"].eq(cutoff + timedelta(minutes=1)).any()


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
    assert stockout_count_plan("tiny") == {"eligible_per_sku": 20, "removed_per_sku": 18}
    assert stockout_count_plan("full") == {
        "eligible_per_sku": 2000,
        "removed_per_sku": 1800,
    }
