"""Deterministic business and data-quality anomaly injection.

This module deliberately contains no random selection.  Each mutation first narrows
its table by the contractual time and business scope, then uses its stable identity
order.  The returned frames are independent copies of the clean input bundle.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from numbers import Integral
from typing import Literal

import pandas as pd  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from governed_analytics.data_generation.dimensions import generate_products
from governed_analytics.data_generation.facts import GeneratedFacts
from governed_analytics.data_generation.models import (
    DatasetScale,
    GeneratorConfig,
    dataset_id_for_config,
)
from governed_analytics.data_generation.vocabulary import SOUTH_REGIONS

_MONEY_ZERO = Decimal("0.00")
_CONVERSION_START = datetime(2026, 6, 8, tzinfo=UTC)
_CONVERSION_END = datetime(2026, 6, 15, tzinfo=UTC)
_REFUND_START = datetime(2026, 5, 4, tzinfo=UTC)
_REFUND_END = datetime(2026, 5, 11, tzinfo=UTC)
_INVENTORY_DAY = datetime(2026, 6, 15, tzinfo=UTC)
_CONTRACT_ANOMALY_IDS = (
    "anomaly_gmv_drop_south_conversion",
    "anomaly_gmv_drop_stockout",
    "anomaly_refund_spike_category",
    "anomaly_inventory_delay",
    "anomaly_duplicate_order_items",
    "anomaly_order_amount_mismatch",
    "anomaly_missing_region",
    "anomaly_refund_exceeds_payment",
)


class ExpectedSignal(BaseModel):
    """A metric-level effect expected from one injected root cause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str
    operator: Literal["decrease", "increase", "equals", "stale"]
    threshold: Decimal | int | str


class AnomalyRecord(BaseModel):
    """Immutable, machine-readable truth for one deterministic anomaly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    anomaly_id: str
    start_at: datetime
    end_at: datetime
    root_cause: str
    affected_keys: tuple[str, ...]
    mutation: dict[str, str | int | float]
    mutated_rows: int = Field(ge=0)
    expected_signals: tuple[ExpectedSignal, ...]

    @field_validator("start_at", "end_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        """Keep truth timestamps unambiguous across JSON and database boundaries."""
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("anomaly timestamps must be UTC-aware")
        return value

    @model_validator(mode="after")
    def validate_half_open_window(self) -> "AnomalyRecord":
        """Reject empty and inverted truth windows at the public model boundary."""
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class AnomalyManifest(BaseModel):
    """The ordered source of truth for all injected anomalies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    anomalies: tuple[AnomalyRecord, ...]

    @model_validator(mode="after")
    def validate_contract_anomaly_order(self) -> "AnomalyManifest":
        """Make manifest completeness, uniqueness and order non-optional for consumers."""
        actual_ids = tuple(record.anomaly_id for record in self.anomalies)
        if actual_ids != _CONTRACT_ANOMALY_IDS:
            raise ValueError("anomaly IDs must exactly match the fixed contract order")
        return self

    def by_id(self, anomaly_id: str) -> AnomalyRecord:
        """Return one record or provide an actionable unknown-ID failure."""
        for item in self.anomalies:
            if item.anomaly_id == anomaly_id:
                return item
        raise KeyError(f"unknown anomaly_id: {anomaly_id}")


@dataclass(frozen=True)
class AnomalyInjectionResult:
    """Mutated fact bundle plus its pre-load, config-derived anomaly truth."""

    orders: pd.DataFrame
    order_items: pd.DataFrame
    payments: pd.DataFrame
    refunds: pd.DataFrame
    web_sessions: pd.DataFrame
    inventory_snapshots: pd.DataFrame
    campaign_attributions: pd.DataFrame
    pipeline_runs: pd.DataFrame
    manifest: AnomalyManifest


def anomaly_count_plan(scale: DatasetScale | str) -> dict[str, int]:
    """Expose the four exact scale-dependent quality mutation quotas for lightweight tests."""
    normalized = DatasetScale(scale)
    if normalized is DatasetScale.TINY:
        return {
            "duplicate_order_items": 20,
            "order_amount_mismatch": 10,
            "missing_region": 10,
            "refund_exceeds_payment": 3,
        }
    return {
        "duplicate_order_items": 2000,
        "order_amount_mismatch": 1000,
        "missing_region": 1000,
        "refund_exceeds_payment": 300,
    }


def stockout_count_plan(scale: DatasetScale | str) -> dict[str, int]:
    """Expose the anchored per-SKU stockout contract without building a full fact bundle."""
    normalized = DatasetScale(scale)
    if normalized is DatasetScale.TINY:
        return {"eligible_per_sku": 20, "removed_per_sku": 18}
    return {"eligible_per_sku": 2000, "removed_per_sku": 1800}


def selected_keys_sha256(keys: list[object] | tuple[object, ...]) -> str:
    """Hash an ordered primary/business-key cohort without expanding a manifest indefinitely."""
    canonical_keys: list[list[str | int | bool]] = []
    for key in keys:
        if isinstance(key, bool):
            canonical_keys.append(["bool", key])
        elif isinstance(key, Integral):
            canonical_keys.append(["int", int(key)])
        elif isinstance(key, str):
            canonical_keys.append(["str", key])
        else:
            raise TypeError(f"unsupported selected key type: {type(key).__name__}")
    canonical = json.dumps(canonical_keys, ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _audited_keys(
    *,
    scope: str,
    selected_keys: list[object] | tuple[object, ...],
    selection: str,
    details: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Build machine-auditable scope, count and canonical-selection metadata."""
    return (
        f"scope={scope}",
        *details,
        f"selected_count={len(selected_keys)}",
        f"selected_keys_sha256={selected_keys_sha256(selected_keys)}",
        f"selection={selection}",
    )


def _in_window(frame: pd.DataFrame, column: str, start: datetime, end: datetime) -> pd.Series:
    return frame[column].between(start, end, inclusive="left")


def _require_count(name: str, selected: pd.DataFrame, expected: int) -> pd.DataFrame:
    if len(selected) < expected:
        raise ValueError(f"{name} needs {expected} eligible rows, found {len(selected)}")
    return selected.iloc[:expected].copy()


def _failed_payment(payments: pd.DataFrame, order_ids: set[int], *, zero_amount: bool) -> None:
    mask = payments["order_id"].isin(order_ids)
    payments.loc[mask, "status"] = "failed"
    payments.loc[mask, "paid_at"] = pd.NaT
    if zero_amount:
        payments.loc[mask, "amount"] = _MONEY_ZERO


def _reconcile_stockout_orders(
    orders: pd.DataFrame,
    items: pd.DataFrame,
    payments: pd.DataFrame,
    affected_order_ids: set[int],
) -> None:
    """Recalculate post-delete order totals without reviving earlier failed/cancelled orders."""
    order_index = orders.set_index("order_id")
    item_totals = items.groupby("order_id")[["gross_amount", "discount_amount", "net_amount"]].sum()
    for order_id in sorted(affected_order_ids):
        row_index = orders.index[orders["order_id"].eq(order_id)][0]
        if order_id not in item_totals.index:
            orders.loc[
                row_index, ["gross_amount", "discount_amount", "shipping_amount", "payable_amount"]
            ] = _MONEY_ZERO
            orders.loc[row_index, "status"] = "cancelled"
            _failed_payment(payments, {order_id}, zero_amount=True)
            continue
        totals = item_totals.loc[order_id]
        orders.loc[row_index, "gross_amount"] = totals["gross_amount"]
        orders.loc[row_index, "discount_amount"] = totals["discount_amount"]
        payable = totals["net_amount"] + order_index.loc[order_id, "shipping_amount"]
        orders.loc[row_index, "payable_amount"] = payable
        payment_mask = payments["order_id"].eq(order_id) & payments["status"].eq("succeeded")
        payments.loc[payment_mask, "amount"] = payable


def _renumber_identities(
    order_items: pd.DataFrame,
    refunds: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Restore loader-compatible contiguous identities and rewire nullable item FKs."""
    order_items = (
        order_items.sort_values("order_item_id", kind="stable").reset_index(drop=True).copy()
    )
    old_item_ids = order_items["order_item_id"].tolist()
    item_id_map = {int(old_id): position for position, old_id in enumerate(old_item_ids, start=1)}
    order_items["order_item_id"] = list(range(1, len(order_items) + 1))

    refunds = refunds.sort_values("refund_id", kind="stable").reset_index(drop=True).copy()
    rewired: list[int | None] = []
    for item_id in refunds["order_item_id"].tolist():
        rewired.append(None if pd.isna(item_id) else item_id_map.get(int(item_id)))
    refunds["order_item_id"] = pd.Series(rewired, dtype="Int64")
    refunds["refund_id"] = list(range(1, len(refunds) + 1))
    refunds["refund_code"] = [f"REF-{refund_id:08d}" for refund_id in refunds["refund_id"]]
    return order_items, refunds


def inject_anomalies(config: GeneratorConfig, base: GeneratedFacts) -> AnomalyInjectionResult:
    """Inject the eight contractual anomalies in one explicit composable order."""
    orders = base.orders.copy(deep=True)
    order_items = base.order_items.copy(deep=True)
    payments = base.payments.copy(deep=True)
    refunds = base.refunds.copy(deep=True)
    web_sessions = base.web_sessions.copy(deep=True)
    inventory_snapshots = base.inventory_snapshots.copy(deep=True)
    campaign_attributions = base.campaign_attributions.copy(deep=True)
    pipeline_runs = base.pipeline_runs.copy(deep=True)
    counts = anomaly_count_plan(config.scale)
    records: list[AnomalyRecord] = []

    # 1. South conversion decline: session time scope, then session identity.
    order_regions = orders.set_index("order_id")["region"]
    conversion_scope = web_sessions[
        web_sessions["converted"]
        & _in_window(web_sessions, "occurred_at", _CONVERSION_START, _CONVERSION_END)
        & web_sessions["order_id"].map(order_regions).isin(SOUTH_REGIONS)
    ].sort_values("session_id", kind="stable")
    flipped = conversion_scope.iloc[: int(len(conversion_scope) * 0.35)]
    flipped_order_ids = {int(value) for value in flipped["order_id"].dropna().tolist()}
    web_sessions.loc[flipped.index, "converted"] = False
    web_sessions.loc[flipped.index, "order_id"] = pd.NA
    web_sessions["order_id"] = web_sessions["order_id"].astype("Int64")
    orders.loc[orders["order_id"].isin(flipped_order_ids), "status"] = "cancelled"
    _failed_payment(payments, flipped_order_ids, zero_amount=False)
    refunds.loc[refunds["order_id"].isin(flipped_order_ids), "status"] = "rejected"
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_gmv_drop_south_conversion",
            start_at=_CONVERSION_START,
            end_at=_CONVERSION_END,
            root_cause="华南地区转化下降",
            affected_keys=_audited_keys(
                scope=(
                    "web_sessions.occurred_at in [2026-06-08T00:00:00Z,"
                    "2026-06-15T00:00:00Z);orders.region in SOUTH_REGIONS="
                    + ",".join(sorted(SOUTH_REGIONS))
                ),
                selected_keys=flipped["session_id"].tolist(),
                selection="web_sessions.session_id asc after scope filter",
            ),
            mutation={
                "conversion_flip_ratio": 0.35,
                "selection": "session_id_ascending",
                "linked_order_action": "cancelled",
                "linked_payment_action": "failed_paid_at_cleared",
            },
            mutated_rows=len(flipped),
            expected_signals=(
                ExpectedSignal(
                    metric_id="conversion_rate",
                    operator="decrease",
                    threshold=(
                        "baseline=[2026-06-01T00:00:00Z,2026-06-08T00:00:00Z);"
                        "dimension=region:SOUTH_REGIONS;conversion_multiplier=0.65"
                    ),
                ),
                ExpectedSignal(
                    metric_id="gmv",
                    operator="decrease",
                    threshold=(
                        "baseline=[2026-06-01T00:00:00Z,2026-06-08T00:00:00Z);"
                        "dimension=region:SOUTH_REGIONS;conversion_multiplier=0.65"
                    ),
                ),
            ),
        )
    )

    # 2. Stockout: item scope is the parent order time plus the two SKU identities.
    item_order_times = orders.set_index("order_id")["ordered_at"]
    stock_scopes: dict[int, pd.DataFrame] = {}
    removed_per_sku: dict[int, pd.DataFrame] = {}
    for product_id in (1, 2):
        sku_scope = order_items[
            order_items["product_id"].eq(product_id)
            & order_items["order_id"]
            .map(item_order_times)
            .between(_CONVERSION_START, _CONVERSION_END, inclusive="left")
        ].sort_values("order_item_id", kind="stable")
        stock_scopes[product_id] = sku_scope
        removed_per_sku[product_id] = sku_scope.iloc[: int(len(sku_scope) * 0.90)]
    removed = pd.concat(tuple(removed_per_sku.values()), ignore_index=False).sort_values(
        "order_item_id", kind="stable"
    )
    removed_item_ids = {int(value) for value in removed["order_item_id"].tolist()}
    affected_orders = {int(value) for value in removed["order_id"].tolist()}
    order_items = order_items[~order_items["order_item_id"].isin(removed_item_ids)].copy()
    removed_refund_rows = refunds["order_item_id"].isin(removed_item_ids)
    linked_refund_rejected_count = int(removed_refund_rows.sum())
    refunds.loc[removed_refund_rows, "status"] = "rejected"
    refunds.loc[removed_refund_rows, "order_item_id"] = pd.NA
    refunds.loc[removed_refund_rows, "refunded_at"] = pd.NaT
    refunds["order_item_id"] = refunds["order_item_id"].astype("Int64")
    inventory_scope = inventory_snapshots[
        inventory_snapshots["snapshot_at"].between(
            _CONVERSION_START, _CONVERSION_END, inclusive="left"
        )
        & inventory_snapshots["product_id"].isin((1, 2))
    ]
    inventory_snapshots.loc[inventory_scope.index, "available_qty"] = 0
    _reconcile_stockout_orders(orders, order_items, payments, affected_orders)
    post_stockout_payment_status = payments.set_index("order_id")["status"]
    no_longer_paid = refunds["order_id"].map(post_stockout_payment_status).ne("succeeded")
    refunds.loc[refunds["order_id"].isin(affected_orders) & no_longer_paid, "status"] = "rejected"
    failed_payment_orders = set(payments.loc[payments["status"].eq("failed"), "order_id"])
    succeeded_refunds_for_failed_payment = refunds["status"].eq("succeeded") & refunds[
        "order_id"
    ].isin(failed_payment_orders)
    refunds.loc[succeeded_refunds_for_failed_payment, "status"] = "rejected"
    refunds.loc[succeeded_refunds_for_failed_payment, "refunded_at"] = pd.NaT
    post_stockout_payment_amount = payments.set_index("order_id")["amount"]
    affected_refunds = refunds["order_id"].isin(affected_orders) & refunds["status"].eq("succeeded")
    exceeds_reconciled_payment = refunds["amount"] > refunds["order_id"].map(
        post_stockout_payment_amount
    )
    refunds.loc[affected_refunds & exceeds_reconciled_payment, "amount"] = refunds.loc[
        affected_refunds & exceeds_reconciled_payment, "order_id"
    ].map(post_stockout_payment_amount)
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_gmv_drop_stockout",
            start_at=_CONVERSION_START,
            end_at=_CONVERSION_END,
            root_cause="SKU-000001 与 SKU-000002 缺货",
            affected_keys=(
                "scope=orders.ordered_at in [2026-06-08T00:00:00Z,"
                "2026-06-15T00:00:00Z);products.sku=SKU-000001",
                f"sku=SKU-000001;eligible_count={len(stock_scopes[1])};"
                f"removed_count={len(removed_per_sku[1])};"
                f"selected_keys_sha256={selected_keys_sha256(removed_per_sku[1]['order_item_id'].tolist())}",
                "scope=orders.ordered_at in [2026-06-08T00:00:00Z,"
                "2026-06-15T00:00:00Z);products.sku=SKU-000002",
                f"sku=SKU-000002;eligible_count={len(stock_scopes[2])};"
                f"removed_count={len(removed_per_sku[2])};"
                f"selected_keys_sha256={selected_keys_sha256(removed_per_sku[2]['order_item_id'].tolist())}",
                f"selected_count={len(removed)}",
                f"selected_keys_sha256={selected_keys_sha256(removed['order_item_id'].tolist())}",
                "selection=order_items.order_item_id asc after scope filter per SKU",
            ),
            mutation={
                "sku_count": 2,
                "item_suppression_ratio": 0.9,
                "selection": "order_item_id_ascending",
                "linked_refund_status_action": "set_rejected",
                "linked_refund_item_fk_action": "clear",
                "linked_refund_timestamp_action": "clear",
                "linked_refund_rejected_count": linked_refund_rejected_count,
                "linked_order_payment_action": "totals_recomputed",
                "sku_000001_eligible_count": len(stock_scopes[1]),
                "sku_000001_removed_count": len(removed_per_sku[1]),
                "sku_000002_eligible_count": len(stock_scopes[2]),
                "sku_000002_removed_count": len(removed_per_sku[2]),
            },
            mutated_rows=len(removed),
            expected_signals=(
                ExpectedSignal(
                    metric_id="gmv",
                    operator="decrease",
                    threshold=(
                        "baseline=[2026-06-01T00:00:00Z,2026-06-08T00:00:00Z);"
                        "dimension=sku:SKU-000001,SKU-000002;item_suppression_ratio=0.90"
                    ),
                ),
                ExpectedSignal(metric_id="stockout_rate", operator="increase", threshold=2),
            ),
        )
    )

    # 3. CAT-018 refund spike: coverage is over distinct legal paid orders in the half-open week.
    product_categories = generate_products(config).set_index("product_id")["category_id"]
    succeeded_orders = set(payments.loc[payments["status"].eq("succeeded"), "order_id"])
    cat_items = order_items[order_items["product_id"].map(product_categories).eq(18)].copy()
    cat_items = cat_items[
        cat_items["order_id"].isin(succeeded_orders)
        & cat_items["order_id"]
        .map(orders.set_index("order_id")["ordered_at"])
        .between(_REFUND_START, _REFUND_END, inclusive="left")
    ].sort_values(["order_id", "order_item_id"], kind="stable")
    eligible_order_ids = cat_items["order_id"].drop_duplicates().tolist()
    target_refund_coverage = int(len(eligible_order_ids) * 0.24)
    existing_refunded_orders = set(
        refunds.loc[
            refunds["status"].eq("succeeded") & refunds["order_id"].isin(eligible_order_ids),
            "order_id",
        ]
    )
    selected_refund_orders = eligible_order_ids[:target_refund_coverage]
    additions: list[dict[str, object]] = []
    payment_by_order = payments.set_index("order_id")
    for order_id in selected_refund_orders:
        if order_id in existing_refunded_orders:
            continue
        item = cat_items[cat_items["order_id"].eq(order_id)].iloc[0]
        payment = payment_by_order.loc[order_id]
        refund_at = max(payment["paid_at"], _REFUND_START)
        if refund_at >= _REFUND_END:
            raise ValueError("eligible refund payment occurs outside the anomaly window")
        amount = min(item["net_amount"], payment["amount"])
        additions.append(
            {
                "refund_id": len(refunds) + len(additions) + 1,
                "refund_code": f"REF-{len(refunds) + len(additions) + 1:08d}",
                "order_id": order_id,
                "order_item_id": int(item["order_item_id"]),
                "status": "succeeded",
                "amount": amount,
                "reason": "商品质量问题",
                "refunded_at": refund_at,
                "created_at": refund_at,
            }
        )
    if additions:
        refunds = pd.concat((refunds, pd.DataFrame(additions)), ignore_index=True)
    refunds["order_item_id"] = pd.Series(refunds["order_item_id"], dtype="Int64")
    orders.loc[orders["order_id"].isin(selected_refund_orders), "status"] = "refunded"
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_refund_spike_category",
            start_at=_REFUND_START,
            end_at=_REFUND_END,
            root_cause="CAT-018 商品质量问题导致退款激增",
            affected_keys=_audited_keys(
                scope=(
                    "orders.ordered_at in [2026-05-04T00:00:00Z,2026-05-11T00:00:00Z);"
                    "products.category_id=18;payments.status=succeeded"
                ),
                selected_keys=[row["order_id"] for row in additions],
                selection="refunds.order_id asc after scope filter",
                details=(
                    f"eligible_order_count={len(eligible_order_ids)}",
                    f"coverage_target_count={target_refund_coverage}",
                ),
            ),
            mutation={
                "category_id": 18,
                "target_refund_coverage": 0.24,
                "target_coverage_count": target_refund_coverage,
                "existing_successful_refund_count": len(existing_refunded_orders),
                "rounding": "floor",
                "linked_order_action": "refunded",
            },
            mutated_rows=len(additions),
            expected_signals=(
                ExpectedSignal(
                    metric_id="refund_rate", operator="increase", threshold=Decimal("2")
                ),
            ),
        )
    )

    # 4. Inventory delay: the clean snapshot is noon, so every post-08:00 row is stale.
    inventory_cutoff = _INVENTORY_DAY + timedelta(hours=8)
    inventory_day_end = _INVENTORY_DAY + timedelta(days=1)
    delayed = inventory_snapshots[
        inventory_snapshots["snapshot_at"].gt(inventory_cutoff)
        & inventory_snapshots["snapshot_at"].lt(inventory_day_end)
    ]
    inventory_snapshots = inventory_snapshots.drop(index=delayed.index).copy()
    run_mask = pipeline_runs["pipeline_name"].eq("inventory") & pipeline_runs[
        "started_at"
    ].dt.date.eq(_INVENTORY_DAY.date())
    pipeline_runs.loc[run_mask, "status"] = "failed"
    pipeline_runs.loc[run_mask, "error_code"] = "INVENTORY_SNAPSHOT_DELAY"
    pipeline_runs.loc[run_mask, "watermark"] = _INVENTORY_DAY
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_inventory_delay",
            start_at=_INVENTORY_DAY,
            end_at=_INVENTORY_DAY + timedelta(days=1),
            root_cause="库存快照延迟且库存管道失败",
            affected_keys=_audited_keys(
                scope=(
                    "inventory_snapshots.snapshot_at in (2026-06-15T08:00:00Z,2026-06-16T00:00:00Z)"
                ),
                selected_keys=[
                    f"{snapshot_at.isoformat()}|{product_id}"
                    for snapshot_at, product_id in zip(
                        delayed["snapshot_at"], delayed["product_id"], strict=True
                    )
                ],
                selection="inventory_snapshots.(snapshot_at,product_id) asc after scope filter",
                details=("pipeline=inventory;pipeline_action=status_changed",),
            ),
            mutation={
                "snapshot_cutoff_hour_utc": 8,
                "pipeline_action": "status_changed",
                "pipeline_status": "failed",
                "watermark": "2026-06-15T00:00:00Z",
                "comparison": "<=",
                "as_of": "2026-06-16T00:00:00Z",
                "cutoff_at": "2026-06-15T08:00:00Z",
            },
            mutated_rows=len(delayed),
            expected_signals=(
                ExpectedSignal(
                    metric_id="inventory_freshness",
                    operator="stale",
                    threshold="watermark_lte=2026-06-15T00:00:00Z",
                ),
            ),
        )
    )

    # 5. Freeze duplicate sources before appending so copied rows can never recursively select.
    duplicate_day = datetime(2026, 4, 10, tzinfo=UTC)
    duplicate_scope = order_items[
        order_items["order_id"]
        .map(orders.set_index("order_id")["ordered_at"])
        .between(duplicate_day, duplicate_day + timedelta(days=1), inclusive="left")
    ].sort_values("order_item_id", kind="stable")
    duplicate_sources = _require_count(
        "duplicate order items", duplicate_scope, counts["duplicate_order_items"]
    )
    duplicate_rows = duplicate_sources.copy()
    next_order_item_id = int(order_items["order_item_id"].max()) + 1
    duplicate_rows["order_item_id"] = list(
        range(next_order_item_id, next_order_item_id + len(duplicate_rows))
    )
    order_items = pd.concat((order_items, duplicate_rows), ignore_index=True)
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_duplicate_order_items",
            start_at=duplicate_day,
            end_at=duplicate_day + timedelta(days=1),
            root_cause="模拟重跑未幂等导致订单明细重复",
            affected_keys=_audited_keys(
                scope="orders.ordered_at in [2026-04-10T00:00:00Z,2026-04-11T00:00:00Z)",
                selected_keys=duplicate_sources["source_line_id"].tolist(),
                selection="order_items.order_item_id asc after scope filter",
            ),
            mutation={
                "copies": len(duplicate_rows),
                "selection": "order_item_id_ascending",
                "duplicate_key": "source_line_id",
            },
            mutated_rows=len(duplicate_rows),
            expected_signals=(
                ExpectedSignal(
                    metric_id="duplicate_order_items",
                    operator="equals",
                    threshold=len(duplicate_rows) * 2,
                ),
            ),
        )
    )

    # 6. Declared header-only mismatch, deliberately leaving lines and payment untouched.
    mismatch_day = datetime(2026, 3, 17, tzinfo=UTC)
    mismatch_scope = orders[
        _in_window(orders, "ordered_at", mismatch_day, mismatch_day + timedelta(days=1))
    ].sort_values("order_id", kind="stable")
    mismatch_orders = _require_count(
        "order amount mismatch", mismatch_scope, counts["order_amount_mismatch"]
    )
    orders.loc[mismatch_orders.index, "payable_amount"] = mismatch_orders[
        "payable_amount"
    ] + Decimal("10.00")
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_order_amount_mismatch",
            start_at=mismatch_day,
            end_at=mismatch_day + timedelta(days=1),
            root_cause="订单金额同步缺陷",
            affected_keys=_audited_keys(
                scope="orders.ordered_at in [2026-03-17T00:00:00Z,2026-03-18T00:00:00Z)",
                selected_keys=mismatch_orders["order_id"].tolist(),
                selection="orders.order_id asc after scope filter",
            ),
            mutation={
                "payable_amount_delta": "10.00",
                "selection": "order_id_ascending",
                "linked_payment_action": "unchanged",
            },
            mutated_rows=len(mismatch_orders),
            expected_signals=(
                ExpectedSignal(
                    metric_id="order_item_reconciliation",
                    operator="equals",
                    threshold=len(mismatch_orders),
                ),
            ),
        )
    )

    # 7. Declared regional nulls are selected only from the contractual business day.
    missing_day = datetime(2026, 2, 12, tzinfo=UTC)
    missing_scope = orders[
        _in_window(orders, "ordered_at", missing_day, missing_day + timedelta(days=1))
    ].sort_values("order_id", kind="stable")
    missing_orders = _require_count("missing region", missing_scope, counts["missing_region"])
    orders.loc[missing_orders.index, "region"] = pd.NA
    orders["region"] = orders["region"].astype("string")
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_missing_region",
            start_at=missing_day,
            end_at=missing_day + timedelta(days=1),
            root_cause="上游地区映射错误",
            affected_keys=_audited_keys(
                scope="orders.ordered_at in [2026-02-12T00:00:00Z,2026-02-13T00:00:00Z)",
                selected_keys=missing_orders["order_id"].tolist(),
                selection="orders.order_id asc after scope filter",
            ),
            mutation={
                "null_count": len(missing_orders),
                "selection": "order_id_ascending",
                "field": "orders.region",
            },
            mutated_rows=len(missing_orders),
            expected_signals=(
                ExpectedSignal(
                    metric_id="region_completeness",
                    operator="equals",
                    threshold=len(missing_orders),
                ),
            ),
        )
    )

    # 8. Keep payment untouched and make exactly the selected refund records individually invalid.
    refund_day = datetime(2026, 5, 20, tzinfo=UTC)
    refund_scope = refunds[
        refunds["status"].eq("succeeded")
        & _in_window(refunds, "refunded_at", refund_day, refund_day + timedelta(days=1))
    ].sort_values("refund_id", kind="stable")
    violating_refunds = _require_count(
        "refund exceeds payment", refund_scope, counts["refund_exceeds_payment"]
    )
    payment_amounts = payments.set_index("order_id")["amount"]
    refunds.loc[violating_refunds.index, "amount"] = violating_refunds["order_id"].map(
        payment_amounts
    ) + Decimal("50.00")
    order_items, refunds = _renumber_identities(order_items, refunds)

    # The existing/generated identities remain contiguous too; retain immutable column order.
    orders = orders.sort_values("order_id", kind="stable").reset_index(drop=True)
    payments = payments.sort_values("payment_id", kind="stable").reset_index(drop=True)
    web_sessions = web_sessions.sort_values("session_id", kind="stable").reset_index(drop=True)
    inventory_snapshots = inventory_snapshots.sort_values(
        "inventory_snapshot_id", kind="stable"
    ).reset_index(drop=True)
    inventory_snapshots["inventory_snapshot_id"] = list(range(1, len(inventory_snapshots) + 1))
    campaign_attributions = campaign_attributions.sort_values(
        "attribution_id", kind="stable"
    ).reset_index(drop=True)
    pipeline_runs = pipeline_runs.sort_values("pipeline_run_id", kind="stable").reset_index(
        drop=True
    )
    records.append(
        AnomalyRecord(
            anomaly_id="anomaly_refund_exceeds_payment",
            start_at=refund_day,
            end_at=refund_day + timedelta(days=1),
            root_cause="退款业务一致性错误",
            affected_keys=_audited_keys(
                scope=(
                    "refunds.refunded_at in [2026-05-20T00:00:00Z,2026-05-21T00:00:00Z);"
                    "refunds.status=succeeded"
                ),
                selected_keys=violating_refunds["refund_id"].tolist(),
                selection="refunds.refund_id asc after scope filter",
            ),
            mutation={
                "payment_amount_delta": "50.00",
                "selection": "refund_id_ascending",
                "linked_payment_action": "unchanged",
            },
            mutated_rows=len(violating_refunds),
            expected_signals=(
                ExpectedSignal(
                    metric_id="refund_payment_consistency",
                    operator="equals",
                    threshold=len(violating_refunds),
                ),
            ),
        )
    )
    return AnomalyInjectionResult(
        orders=orders,
        order_items=order_items,
        payments=payments,
        refunds=refunds,
        web_sessions=web_sessions,
        inventory_snapshots=inventory_snapshots,
        campaign_attributions=campaign_attributions,
        pipeline_runs=pipeline_runs,
        manifest=AnomalyManifest(
            dataset_id=dataset_id_for_config(config), anomalies=tuple(records)
        ),
    )
