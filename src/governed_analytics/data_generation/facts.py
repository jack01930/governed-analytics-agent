"""Deterministic, business-consistent pre-anomaly ecommerce fact generation."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from governed_analytics.data_generation.dimensions import (
    cents_to_decimal,
    generate_campaigns,
    generate_customers,
    generate_products,
)
from governed_analytics.data_generation.models import DatasetScale, GeneratorConfig
from governed_analytics.data_generation.randomness import named_rng
from governed_analytics.data_generation.vocabulary import CHANNELS, SOUTH_REGIONS

_ORDER_STATUSES = ("placed", "paid", "completed", "cancelled")
_ORDER_STATUS_WEIGHTS = (0.03, 0.12, 0.77, 0.08)
_ITEM_COUNTS = (1, 2, 3, 4, 5)
_ITEM_COUNT_WEIGHTS = (0.35, 0.35, 0.18, 0.08, 0.04)
_QUANTITIES = (1, 2, 3)
_QUANTITY_WEIGHTS = (0.78, 0.17, 0.05)
_DISCOUNT_RATES = (0, 5, 10, 15)
_DISCOUNT_WEIGHTS = (0.55, 0.20, 0.20, 0.05)
_CHANNEL_WEIGHTS = (0.30, 0.25, 0.20, 0.10, 0.15)
_PAYMENT_PROVIDERS = ("alipay", "wechat_pay", "card")
_REFUND_REASONS = ("商品质量问题", "不再需要", "配送延迟", "规格不符")
_Q2_2026_START = datetime(2026, 4, 1, tzinfo=UTC)
_Q2_2026_END = datetime(2026, 7, 1, tzinfo=UTC)


@dataclass(frozen=True)
class GeneratedFacts:
    """Canonical, clean fact frames in PostgreSQL schema column order."""

    orders: pd.DataFrame
    order_items: pd.DataFrame
    payments: pd.DataFrame
    refunds: pd.DataFrame
    web_sessions: pd.DataFrame
    inventory_snapshots: pd.DataFrame
    campaign_attributions: pd.DataFrame
    pipeline_runs: pd.DataFrame


def daily_order_weights(start_at: datetime, end_at: datetime) -> np.ndarray:
    """Return normalized daily weights for the specified half-open UTC interval."""
    days = pd.date_range(start_at, end_at, freq="D", inclusive="left")
    weights = np.ones(len(days), dtype=float)
    weights[days.month == 12] *= 1.25
    weights[days.month == 6] *= 0.92
    weights[days.dayofweek >= 4] *= 1.15
    return np.asarray(weights / weights.sum())


def _sum_cents_by_order(
    order_ids: np.ndarray, cents: np.ndarray, *, order_count: int
) -> np.ndarray:
    """Aggregate integer-cent values by identity without float-weighted NumPy APIs."""
    totals = np.zeros(order_count + 1, dtype=np.int64)
    np.add.at(totals, order_ids, cents)
    return totals


def _quota(config: GeneratorConfig, tiny_count: int) -> int:
    """Scale a Task 4 anchor quota while preserving the exact tiny/full contracts."""
    if config.scale is DatasetScale.TINY:
        return tiny_count
    return tiny_count * config.orders // 3000


def _anchor_order_ids(config: GeneratorConfig) -> dict[str, np.ndarray]:
    """Reserve disjoint, ascending-PK clean cohorts for every future anomaly filter."""
    counts = {
        "duplicate": _quota(config, 20),
        "amount_mismatch": _quota(config, 10),
        "missing_region": _quota(config, 10),
        "category_refund": _quota(config, 25),
        "refund_exceeds_payment": _quota(config, 3),
        "south_sku": _quota(config, 40),
        "consumer_previous": _quota(config, 100),
        "campaign_q2": min(5, config.campaigns),
    }
    cursor = 1
    anchors: dict[str, np.ndarray] = {}
    for name, count in counts.items():
        anchors[name] = np.arange(cursor, cursor + count, dtype=int)
        cursor += count
    if cursor - 1 > config.orders:
        raise ValueError("order count is too small for the required anomaly anchor cohorts")
    return anchors


def _date_for_anchor(anchor_name: str) -> date:
    """Return the deterministic business date belonging to one future anomaly cohort."""
    dates = {
        "duplicate": date(2026, 4, 10),
        "amount_mismatch": date(2026, 3, 17),
        "missing_region": date(2026, 2, 12),
        "category_refund": date(2026, 5, 4),
        "refund_exceeds_payment": date(2026, 5, 20),
        "south_sku": date(2026, 6, 8),
        "consumer_previous": date(2026, 6, 1),
    }
    return dates[anchor_name]


def _generate_orders_and_items(
    config: GeneratorConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, np.ndarray]]:
    """Generate order headers and line items, retaining integer cents until frame construction."""
    customers = generate_customers(config)
    products = generate_products(config)
    campaigns = generate_campaigns(config)
    anchors = _anchor_order_ids(config)
    order_ids = np.arange(1, config.orders + 1, dtype=int)
    day_starts = pd.date_range(config.start_at, config.end_at, freq="D", inclusive="left")
    timestamp_rng = named_rng(config.seed, "facts.orders.timestamps")
    day_indexes = timestamp_rng.choice(
        len(day_starts), size=config.orders, p=daily_order_weights(config.start_at, config.end_at)
    )
    seconds = timestamp_rng.integers(0, 24 * 60 * 60, size=config.orders)
    ordered_at = [
        day_starts[int(day_index)].to_pydatetime() + timedelta(seconds=int(second))
        for day_index, second in zip(day_indexes, seconds, strict=True)
    ]
    q2_campaigns = campaigns[
        (campaigns["start_at"] < _Q2_2026_END) & (campaigns["end_at"] > _Q2_2026_START)
    ]
    campaign_anchor_ids = anchors["campaign_q2"]
    if len(q2_campaigns) < len(campaign_anchor_ids):
        raise ValueError(
            "campaign dimensions have insufficient Q2 campaigns for attribution anchors"
        )
    for name, ids in anchors.items():
        if name == "campaign_q2":
            for order_id, campaign in zip(
                ids, q2_campaigns.iloc[: len(ids)].itertuples(index=False), strict=True
            ):
                ordered_at[int(order_id) - 1] = campaign.start_at + timedelta(minutes=30)
            continue
        anchored_day = _date_for_anchor(name)
        for order_id in ids:
            hour_limit = 10 if name == "refund_exceeds_payment" else 24
            offset = int(timestamp_rng.integers(0, hour_limit * 60 * 60))
            ordered_at[int(order_id) - 1] = datetime(
                anchored_day.year, anchored_day.month, anchored_day.day, tzinfo=UTC
            ) + timedelta(seconds=offset)

    customer_rng = named_rng(config.seed, "facts.orders.customers")
    customer_ids = customer_rng.integers(1, config.customers + 1, size=config.orders)
    south_customer_ids = customers.loc[
        customers["region"].isin(SOUTH_REGIONS), "customer_id"
    ].to_numpy(dtype=int)
    if len(south_customer_ids) == 0:
        raise ValueError("customer dimensions have no South-region customer for conversion anchors")
    south_anchor_ids = np.concatenate((anchors["south_sku"], anchors["consumer_previous"]))
    customer_ids[south_anchor_ids - 1] = np.resize(south_customer_ids, len(south_anchor_ids))
    customer_regions = customers.set_index("customer_id").loc[customer_ids, "region"].tolist()
    channel_rng = named_rng(config.seed, "facts.orders.channels")
    channels = channel_rng.choice(CHANNELS, size=config.orders, p=_CHANNEL_WEIGHTS)
    for order_id, campaign in zip(
        campaign_anchor_ids,
        q2_campaigns.iloc[: len(campaign_anchor_ids)].itertuples(index=False),
        strict=True,
    ):
        channels[int(order_id) - 1] = campaign.channel

    item_rng = named_rng(config.seed, "facts.order_items")
    item_counts = item_rng.choice(_ITEM_COUNTS, size=config.orders, p=_ITEM_COUNT_WEIGHTS).astype(
        int
    )
    for ids in anchors.values():
        item_counts[ids - 1] = np.maximum(item_counts[ids - 1], 1)
    item_order_ids = np.repeat(order_ids, item_counts)
    item_ids = np.arange(1, len(item_order_ids) + 1, dtype=int)
    product_ids = item_rng.integers(1, config.products + 1, size=len(item_ids))
    first_item_indexes = np.concatenate(([0], np.cumsum(item_counts)[:-1]))
    category_18_products = products.loc[products["category_id"] == 18, "product_id"].to_numpy(
        dtype=int
    )
    if len(category_18_products) == 0:
        raise ValueError("product dimensions have no CAT-018 product for refund anchors")
    for index, order_id in enumerate(anchors["category_refund"]):
        product_ids[first_item_indexes[int(order_id) - 1]] = category_18_products[
            index % len(category_18_products)
        ]
    for consumer_sku_ids in (anchors["south_sku"], anchors["consumer_previous"]):
        per_sku = len(consumer_sku_ids) // 2
        for index, order_id in enumerate(consumer_sku_ids):
            product_ids[first_item_indexes[int(order_id) - 1]] = (
                1 if index < per_sku else 2
            )

    quantities = item_rng.choice(_QUANTITIES, size=len(item_ids), p=_QUANTITY_WEIGHTS).astype(int)
    discount_rates = item_rng.choice(
        _DISCOUNT_RATES, size=len(item_ids), p=_DISCOUNT_WEIGHTS
    ).astype(int)
    product_prices = products.set_index("product_id").loc[product_ids, "list_price"].tolist()
    unit_price_cents = np.array([int(price * 100) for price in product_prices], dtype=np.int64)
    gross_cents = unit_price_cents * quantities
    discount_cents = gross_cents * discount_rates // 100
    net_cents = gross_cents - discount_cents
    gross_by_order = _sum_cents_by_order(item_order_ids, gross_cents, order_count=config.orders)
    discount_by_order = _sum_cents_by_order(
        item_order_ids, discount_cents, order_count=config.orders
    )
    shipping_rng = named_rng(config.seed, "facts.orders.shipping")
    shipping_cents = shipping_rng.choice((0, 600, 1200), size=config.orders, p=(0.25, 0.60, 0.15))
    payable_cents = gross_by_order[1:] - discount_by_order[1:]
    payable_cents += shipping_cents

    status_rng = named_rng(config.seed, "facts.orders.statuses")
    planned_statuses = status_rng.choice(
        _ORDER_STATUSES, size=config.orders, p=_ORDER_STATUS_WEIGHTS
    )
    payment_outcome_rng = named_rng(config.seed, "facts.payments.outcomes")
    success_probabilities = np.select(
        [planned_statuses == "cancelled", planned_statuses == "placed"], [0.05, 0.30], default=0.97
    )
    payment_succeeded = payment_outcome_rng.random(config.orders) < success_probabilities
    payment_succeeded[anchors["refund_exceeds_payment"] - 1] = True
    payment_succeeded[anchors["consumer_previous"] - 1] = True
    final_statuses = np.where(
        payment_succeeded,
        np.where(planned_statuses == "completed", "completed", "paid"),
        np.where(planned_statuses == "cancelled", "cancelled", "placed"),
    )

    orders = pd.DataFrame(
        {
            "order_id": order_ids.tolist(),
            "order_code": [f"ORD-{order_id:08d}" for order_id in order_ids],
            "customer_id": customer_ids.tolist(),
            "status": final_statuses.tolist(),
            "ordered_at": ordered_at,
            "region": customer_regions,
            "channel": channels.tolist(),
            "currency": ["CNY"] * config.orders,
            "gross_amount": [cents_to_decimal(int(value)) for value in gross_by_order[1:]],
            "discount_amount": [cents_to_decimal(int(value)) for value in discount_by_order[1:]],
            "shipping_amount": [cents_to_decimal(int(value)) for value in shipping_cents],
            "payable_amount": [cents_to_decimal(int(value)) for value in payable_cents],
            "updated_at": ordered_at,
        }
    )
    items = pd.DataFrame(
        {
            "order_item_id": item_ids.tolist(),
            "source_line_id": [f"LINE-{item_id:09d}" for item_id in item_ids],
            "order_id": item_order_ids.tolist(),
            "product_id": product_ids.tolist(),
            "quantity": quantities.tolist(),
            "unit_price": [cents_to_decimal(int(value)) for value in unit_price_cents],
            "discount_amount": [cents_to_decimal(int(value)) for value in discount_cents],
            "gross_amount": [cents_to_decimal(int(value)) for value in gross_cents],
            "net_amount": [cents_to_decimal(int(value)) for value in net_cents],
        }
    )
    return orders, items, anchors


def _generate_payments(config: GeneratorConfig, orders: pd.DataFrame) -> pd.DataFrame:
    """Generate exactly one reconciled payment attempt for every order."""
    timing_rng = named_rng(config.seed, "facts.payments.timing")
    provider_rng = named_rng(config.seed, "facts.payments.providers")
    payment_ids = np.arange(1, len(orders) + 1, dtype=int)
    succeeded = orders["status"].isin(("paid", "completed", "refunded")).to_numpy()
    ordered_at = orders["ordered_at"].tolist()
    delays = timing_rng.integers(60, 2 * 60 * 60, size=len(orders))
    latest_paid_at = config.end_at - timedelta(seconds=1)
    paid_at = [
        min(order_time + timedelta(seconds=int(delay)), latest_paid_at) if is_succeeded else None
        for order_time, delay, is_succeeded in zip(ordered_at, delays, succeeded, strict=True)
    ]
    return pd.DataFrame(
        {
            "payment_id": payment_ids.tolist(),
            "payment_code": [f"PAY-{payment_id:08d}" for payment_id in payment_ids],
            "order_id": orders["order_id"].tolist(),
            "status": np.where(succeeded, "succeeded", "failed").tolist(),
            "provider": provider_rng.choice(_PAYMENT_PROVIDERS, size=len(orders)).tolist(),
            "amount": orders["payable_amount"].tolist(),
            "paid_at": paid_at,
            "created_at": ordered_at,
        }
    )


def _generate_refunds(
    config: GeneratorConfig,
    orders: pd.DataFrame,
    items: pd.DataFrame,
    payments: pd.DataFrame,
    anchors: dict[str, np.ndarray],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate successful, bounded refunds and reconcile the corresponding order status."""
    rng = named_rng(config.seed, "facts.refunds")
    succeeded_order_ids = payments.loc[payments["status"] == "succeeded", "order_id"].to_numpy(
        dtype=int
    )
    selected = succeeded_order_ids[rng.random(len(succeeded_order_ids)) < 0.06]
    selected = np.unique(np.concatenate((selected, anchors["refund_exceeds_payment"])))
    selected.sort()
    items_by_order = items.groupby("order_id", sort=False)["order_item_id"].first()
    item_rows = items.set_index("order_item_id")
    payment_rows = payments.set_index("order_id")
    refund_ids = np.arange(1, len(selected) + 1, dtype=int)
    amounts = []
    refunded_at = []
    anchor_ids = set(anchors["refund_exceeds_payment"].tolist())
    for order_id in selected:
        item_id = int(items_by_order.loc[order_id])
        item_cents = int(item_rows.loc[item_id, "net_amount"] * 100)
        amounts.append(cents_to_decimal(int(rng.integers(1, item_cents + 1))))
        paid_time = payment_rows.loc[order_id, "paid_at"]
        if order_id in anchor_ids:
            refunded_at.append(datetime(2026, 5, 20, 18, tzinfo=UTC))
        else:
            refunded_at.append(
                min(
                    paid_time + timedelta(seconds=int(rng.integers(60, 12 * 60 * 60))),
                    config.end_at - timedelta(seconds=1),
                )
            )
    orders = orders.copy()
    orders.loc[orders["order_id"].isin(selected), "status"] = "refunded"
    refunds = pd.DataFrame(
        {
            "refund_id": refund_ids.tolist(),
            "refund_code": [f"REF-{refund_id:08d}" for refund_id in refund_ids],
            "order_id": selected.tolist(),
            "order_item_id": pd.Series(
                [int(items_by_order.loc[order_id]) for order_id in selected], dtype="Int64"
            ),
            "status": ["succeeded"] * len(selected),
            "amount": amounts,
            "reason": rng.choice(_REFUND_REASONS, size=len(selected)).tolist(),
            "refunded_at": refunded_at,
            "created_at": refunded_at,
        }
    )
    return orders, refunds


def _generate_web_sessions(config: GeneratorConfig, orders: pd.DataFrame) -> pd.DataFrame:
    """Generate one converted session per order plus unconverted baseline browsing sessions."""
    count = config.orders * (5 if config.scale is DatasetScale.TINY else 4)
    rng = named_rng(config.seed, "facts.web_sessions")
    session_ids = np.arange(1, count + 1, dtype=int)
    order_ids = orders["order_id"].to_numpy(dtype=int)
    ordered_at = orders["ordered_at"].tolist()
    conversion_offsets = rng.integers(0, 6 * 60 * 60, size=config.orders)
    converted_at = [
        max(config.start_at, order_time - timedelta(seconds=int(offset)))
        for order_time, offset in zip(ordered_at, conversion_offsets, strict=True)
    ]
    remainder = count - config.orders
    unconverted_customer_ids: list[int | None] = []
    browsing_customers = rng.integers(1, config.customers + 1, size=remainder)
    null_customers = rng.random(remainder) < 0.20
    for customer_id, is_null in zip(browsing_customers, null_customers, strict=True):
        unconverted_customer_ids.append(None if is_null else int(customer_id))
    day_indexes = rng.integers(0, (config.end_at - config.start_at).days, size=remainder)
    seconds = rng.integers(0, 24 * 60 * 60, size=remainder)
    browsing_at = [
        config.start_at + timedelta(days=int(day_index), seconds=int(second))
        for day_index, second in zip(day_indexes, seconds, strict=True)
    ]
    customer_ids = pd.Series(
        orders["customer_id"].tolist() + unconverted_customer_ids, dtype="Int64"
    )
    nullable_order_ids = pd.Series(order_ids.tolist() + [None] * remainder, dtype="Int64")
    return pd.DataFrame(
        {
            "session_id": session_ids.tolist(),
            "session_code": [f"SES-{session_id:09d}" for session_id in session_ids],
            "customer_id": customer_ids,
            "order_id": nullable_order_ids,
            "channel": orders["channel"].tolist()
            + rng.choice(CHANNELS, size=remainder, p=_CHANNEL_WEIGHTS).tolist(),
            "occurred_at": converted_at + browsing_at,
            "converted": [True] * config.orders + [False] * remainder,
            "duration_seconds": rng.integers(15, 3601, size=count).tolist(),
        }
    )


def _generate_inventory_snapshots(config: GeneratorConfig) -> pd.DataFrame:
    """Vectorize one midday inventory snapshot per product and business day."""
    rng = named_rng(config.seed, "facts.inventory")
    days = pd.date_range(config.start_at, config.end_at, freq="D", inclusive="left")
    day_count = len(days)
    product_ids = np.arange(1, config.products + 1, dtype=int)
    baseline = rng.integers(0, 501, size=config.products)
    replenishment_noise = rng.integers(-80, 81, size=(day_count, config.products))
    available = np.clip(baseline[None, :] + replenishment_noise, 0, None).ravel()
    reserved = rng.integers(0, 51, size=(day_count, config.products)).ravel()
    snapshot_at = [
        day.to_pydatetime() + timedelta(hours=12) for day in days for _ in range(config.products)
    ]
    row_count = day_count * config.products
    return pd.DataFrame(
        {
            "inventory_snapshot_id": list(range(1, row_count + 1)),
            "snapshot_at": snapshot_at,
            "product_id": np.tile(product_ids, day_count).tolist(),
            "available_qty": available.tolist(),
            "reserved_qty": reserved.tolist(),
        }
    )


def _generate_campaign_attributions(config: GeneratorConfig, orders: pd.DataFrame) -> pd.DataFrame:
    """Attribute in-window, channel-aligned order revenue to non-overlapping campaigns."""
    campaigns = generate_campaigns(config)
    rows: list[dict[str, object]] = []
    for campaign in campaigns.itertuples(index=False):
        eligible = orders[
            orders["ordered_at"].between(campaign.start_at, campaign.end_at, inclusive="left")
            & orders["channel"].eq(campaign.channel)
        ]
        for order in eligible.itertuples(index=False):
            rows.append(
                {
                    "campaign_id": campaign.campaign_id,
                    "order_id": order.order_id,
                    "attributed_revenue": order.payable_amount,
                    "attributed_at": order.ordered_at,
                }
            )
    for attribution_id, row in enumerate(rows, start=1):
        row["attribution_id"] = attribution_id
    frame = pd.DataFrame(
        rows,
        columns=[
            "attribution_id",
            "campaign_id",
            "order_id",
            "attributed_revenue",
            "attributed_at",
        ],
    )
    return frame


def _generate_pipeline_runs(config: GeneratorConfig) -> pd.DataFrame:
    """Generate three succeeded daily pipeline records with end-of-day watermarks."""
    days = pd.date_range(config.start_at, config.end_at, freq="D", inclusive="left")
    rows: list[dict[str, object]] = []
    pipeline_row_counts = {
        "orders": config.orders,
        "inventory": config.products,
        "sessions": config.orders,
    }
    for day in days:
        started_at = day.to_pydatetime() + timedelta(minutes=5)
        for pipeline_name in ("orders", "inventory", "sessions"):
            rows.append(
                {
                    "pipeline_name": pipeline_name,
                    "started_at": started_at,
                    "finished_at": started_at + timedelta(minutes=4),
                    "status": "succeeded",
                    "watermark": day.to_pydatetime() + timedelta(days=1),
                    "row_count": pipeline_row_counts[pipeline_name],
                    "error_code": None,
                }
            )
    for pipeline_run_id, row in enumerate(rows, start=1):
        row["pipeline_run_id"] = pipeline_run_id
    frame = pd.DataFrame(
        rows,
        columns=[
            "pipeline_run_id",
            "pipeline_name",
            "started_at",
            "finished_at",
            "status",
            "watermark",
            "row_count",
            "error_code",
        ],
    )
    frame["row_count"] = pd.Series(frame["row_count"], dtype="Int64")
    return frame


def generate_base_facts(config: GeneratorConfig) -> GeneratedFacts:
    """Return all eight deterministic, clean fact frames for a generator configuration."""
    orders, order_items, anchors = _generate_orders_and_items(config)
    payments = _generate_payments(config, orders)
    orders, refunds = _generate_refunds(config, orders, order_items, payments, anchors)
    web_sessions = _generate_web_sessions(config, orders)
    inventory_snapshots = _generate_inventory_snapshots(config)
    campaign_attributions = _generate_campaign_attributions(config, orders)
    pipeline_runs = _generate_pipeline_runs(config)
    return GeneratedFacts(
        orders=orders.sort_values("order_id", ignore_index=True),
        order_items=order_items.sort_values("order_item_id", ignore_index=True),
        payments=payments.sort_values("payment_id", ignore_index=True),
        refunds=refunds.sort_values("refund_id", ignore_index=True),
        web_sessions=web_sessions.sort_values("session_id", ignore_index=True),
        inventory_snapshots=inventory_snapshots.sort_values(
            "inventory_snapshot_id", ignore_index=True
        ),
        campaign_attributions=campaign_attributions.sort_values(
            "attribution_id", ignore_index=True
        ),
        pipeline_runs=pipeline_runs.sort_values("pipeline_run_id", ignore_index=True),
    )
