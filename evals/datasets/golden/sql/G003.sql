-- G003 metric_version=1.0.0
with previous_week as (
  select o.region as region, sum(oi.net_amount) as gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'
  group by o.region
), current_week as (
  select o.region as region, sum(oi.net_amount) as gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-08T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'
  group by o.region
)
select coalesce(previous_week.region, current_week.region) as region,
  coalesce(previous_week.gmv, 0) - coalesce(current_week.gmv, 0) as gmv_loss
from previous_week
full outer join current_week on current_week.region = previous_week.region
where coalesce(previous_week.gmv, 0) > coalesce(current_week.gmv, 0)
order by gmv_loss desc, region asc
limit 5;
