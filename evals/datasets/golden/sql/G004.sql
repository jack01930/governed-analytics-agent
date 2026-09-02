-- G004 metric_version=1.0.0
with previous_week as (
  select p.sku as sku, sum(oi.net_amount) as gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join products as p on p.product_id = oi.product_id
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'
  group by p.sku
), current_week as (
  select p.sku as sku, sum(oi.net_amount) as gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join products as p on p.product_id = oi.product_id
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-08T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'
  group by p.sku
)
select coalesce(previous_week.sku, current_week.sku) as sku,
  coalesce(previous_week.gmv, 0) - coalesce(current_week.gmv, 0) as gmv_loss
from previous_week
full outer join current_week on current_week.sku = previous_week.sku
where coalesce(previous_week.gmv, 0) > coalesce(current_week.gmv, 0)
order by gmv_loss desc, sku asc
limit 5;
