-- G019 metric_version=1.0.0
with order_item_totals as (
  select oi.order_id as order_id, sum(oi.net_amount) as item_net_amount
  from order_items as oi
  group by oi.order_id
)
select count(*) as mismatched_orders
from orders as o
join order_item_totals as oit on oit.order_id = o.order_id
where o.ordered_at >= timestamptz '2026-03-17T00:00:00Z'
  and o.ordered_at < timestamptz '2026-03-18T00:00:00Z'
  and abs(o.payable_amount - (oit.item_net_amount + o.shipping_amount)) > 0.01;
