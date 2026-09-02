"""Readonly semantic sanity checks for the first governed metric catalog."""

from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any

import pytest
from sqlalchemy import text

from governed_analytics.config import DatabaseSettings
from governed_analytics.persistence.database import create_async_database_engine

WINDOW = {
    "start": datetime(2026, 6, 1, tzinfo=UTC),
    "end": datetime(2026, 7, 1, tzinfo=UTC),
}

METRIC_QUERIES = {
    "gmv": """
        select coalesce(sum(oi.net_amount), 0) as value
        from orders o join order_items oi on oi.order_id = o.order_id
        where o.ordered_at >= :start and o.ordered_at < :end
          and o.status in ('paid', 'completed', 'refunded')
    """,
    "paid_gmv": """
        select coalesce(sum(p.amount), 0) as value
        from payments p
        where p.paid_at >= :start and p.paid_at < :end and p.status = 'succeeded'
    """,
    "net_revenue": """
        with order_payments as (
            select p.order_id, sum(p.amount) as amount
            from payments p join orders o on o.order_id = p.order_id
            where o.ordered_at >= :start and o.ordered_at < :end and p.status = 'succeeded'
            group by p.order_id
        ), order_refunds as (
            select r.order_id, sum(r.amount) as amount
            from refunds r join orders o on o.order_id = r.order_id
            where o.ordered_at >= :start and o.ordered_at < :end and r.status = 'succeeded'
            group by r.order_id
        )
        select coalesce((select sum(amount) from order_payments), 0)
             - coalesce((select sum(amount) from order_refunds), 0) as value
    """,
    "valid_order_count": """
        select count(distinct o.order_id) as value
        from orders o
        where o.ordered_at >= :start and o.ordered_at < :end
          and o.status in ('paid', 'completed', 'refunded')
    """,
    "average_order_value": """
        select coalesce(sum(oi.net_amount), 0) / nullif(count(distinct o.order_id), 0) as value
        from orders o join order_items oi on oi.order_id = o.order_id
        where o.ordered_at >= :start and o.ordered_at < :end
          and o.status in ('paid', 'completed', 'refunded')
    """,
    "payment_success_rate": """
        select count(*) filter (where p.status = 'succeeded')::numeric
             / nullif(count(*), 0) as value
        from payments p where p.created_at >= :start and p.created_at < :end
    """,
    "refund_amount": """
        select coalesce(sum(r.amount), 0) as value
        from refunds r
        where r.refunded_at >= :start and r.refunded_at < :end and r.status = 'succeeded'
    """,
    "refund_rate": """
        with order_payments as (
            select p.order_id, sum(p.amount) as amount
            from payments p join orders o on o.order_id = p.order_id
            where o.ordered_at >= :start and o.ordered_at < :end and p.status = 'succeeded'
            group by p.order_id
        ), order_refunds as (
            select r.order_id, sum(r.amount) as amount
            from refunds r join orders o on o.order_id = r.order_id
            where o.ordered_at >= :start and o.ordered_at < :end and r.status = 'succeeded'
            group by r.order_id
        )
        select coalesce((select sum(amount) from order_refunds), 0)
             / nullif((select sum(amount) from order_payments), 0) as value
    """,
    "active_customers": """
        select count(distinct o.customer_id) as value
        from orders o
        where o.ordered_at >= :start and o.ordered_at < :end
          and o.status in ('paid', 'completed', 'refunded')
    """,
    "new_customers": """
        select count(distinct c.customer_id) as value
        from customers c where c.registered_at >= :start and c.registered_at < :end
    """,
    "repeat_purchase_rate": """
        with active_customers as (
            select distinct o.customer_id
            from orders o
            where o.ordered_at >= :start and o.ordered_at < :end
              and o.status in ('paid', 'completed', 'refunded')
        ), lifetime_orders as (
            select o.customer_id
            from orders o
            where o.ordered_at < :end and o.status in ('paid', 'completed', 'refunded')
            group by o.customer_id having count(distinct o.order_id) >= 2
        )
        select count(lo.customer_id)::numeric / nullif(count(ac.customer_id), 0) as value
        from active_customers ac left join lifetime_orders lo on lo.customer_id = ac.customer_id
    """,
    "customer_acquisition_cost": """
        with interval_attributions as (
            select a.campaign_id, o.customer_id
            from campaign_attributions a join orders o on o.order_id = a.order_id
            where a.attributed_at >= :start and a.attributed_at < :end
        ), campaign_spend as (
            select mc.campaign_id, mc.spend
            from marketing_campaigns mc
            join (select distinct campaign_id from interval_attributions) ia
              on ia.campaign_id = mc.campaign_id
        )
        select coalesce((select sum(spend) from campaign_spend), 0)
             / nullif((select count(distinct customer_id) from interval_attributions), 0) as value
    """,
    "conversion_rate": """
        select count(*) filter (where s.converted)::numeric / nullif(count(*), 0) as value
        from web_sessions s where s.occurred_at >= :start and s.occurred_at < :end
    """,
    "stockout_rate": """
        select count(*) filter (where i.available_qty = 0)::numeric / nullif(count(*), 0) as value
        from inventory_snapshots i where i.snapshot_at >= :start and i.snapshot_at < :end
    """,
    "campaign_roi": """
        with interval_attributions as (
            select a.campaign_id, a.attributed_revenue
            from campaign_attributions a
            where a.attributed_at >= :start and a.attributed_at < :end
        ), campaign_spend as (
            select mc.campaign_id, mc.spend
            from marketing_campaigns mc
            join (select distinct campaign_id from interval_attributions) ia
              on ia.campaign_id = mc.campaign_id
        )
        select (coalesce((select sum(attributed_revenue) from interval_attributions), 0)
              - coalesce((select sum(spend) from campaign_spend), 0))
             / nullif((select sum(spend) from campaign_spend), 0) as value
    """,
}

MONEY_OR_COUNT = frozenset(
    {
        "gmv",
        "paid_gmv",
        "net_revenue",
        "valid_order_count",
        "average_order_value",
        "refund_amount",
        "active_customers",
        "new_customers",
    }
)
PROBABILITY_RATIOS = frozenset({
    "payment_success_rate",
    "refund_rate",
    "repeat_purchase_rate",
    "conversion_rate",
    "stockout_rate",
})


async def _readonly_connection_state(connection: Any) -> None:
    await connection.execute(text("set transaction read only"))
    await connection.execute(text("set local statement_timeout = '10s'"))
    await connection.execute(text("set local search_path = public, pg_catalog"))
    await connection.execute(text("set local time zone 'UTC'"))
    result = await connection.execute(
        text(
            "select current_user, current_setting('transaction_read_only'), "
            "current_setting('statement_timeout'), current_setting('search_path'), "
            "current_setting('TimeZone')"
        )
    )
    assert result.one() == (
        "analytics_readonly",
        "on",
        "10s",
        "public, pg_catalog",
        "UTC",
    )


def _assert_metric_value(metric_id: str, value: Decimal | int | None) -> None:
    if metric_id in MONEY_OR_COUNT:
        assert value is not None and value >= 0
    elif metric_id in PROBABILITY_RATIOS:
        assert value is None or Decimal(0) <= value <= Decimal(1)
    elif metric_id == "customer_acquisition_cost":
        assert value is None or value >= 0
    else:
        assert metric_id == "campaign_roi"
        assert value is None or Decimal(value).is_finite()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("metric_id", tuple(METRIC_QUERIES))
async def test_each_metric_query_is_parameterized_and_readonly(metric_id: str) -> None:
    settings = DatabaseSettings()  # type: ignore[call-arg]
    engine = create_async_database_engine(settings)
    try:
        async with engine.connect() as connection:
            try:
                await _readonly_connection_state(connection)
                started = perf_counter()
                result = await connection.execute(text(METRIC_QUERIES[metric_id]), WINDOW)
                elapsed = perf_counter() - started
                value = result.scalar_one()
                assert elapsed < 10
                _assert_metric_value(metric_id, value)
            finally:
                await connection.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_campaign_spend_is_preaggregated_before_cac_and_roi() -> None:
    settings = DatabaseSettings()  # type: ignore[call-arg]
    engine = create_async_database_engine(settings)
    comparison_query = text("""
        with interval_attributions as (
            select a.campaign_id
            from campaign_attributions a
            where a.attributed_at >= :start and a.attributed_at < :end
        ), preaggregated as (
            select coalesce(sum(mc.spend), 0) as spend
            from marketing_campaigns mc
            join (select distinct campaign_id from interval_attributions) ia
              on ia.campaign_id = mc.campaign_id
        ), multiplied as (
            select coalesce(sum(mc.spend), 0) as spend
            from marketing_campaigns mc join interval_attributions ia
              on ia.campaign_id = mc.campaign_id
        )
        select preaggregated.spend, multiplied.spend from preaggregated cross join multiplied
    """)
    try:
        async with engine.connect() as connection:
            try:
                await _readonly_connection_state(connection)
                result = await connection.execute(comparison_query, WINDOW)
                preaggregated, multiplied = result.one()
                assert preaggregated >= 0
                assert multiplied >= preaggregated
            finally:
                await connection.rollback()
    finally:
        await engine.dispose()
