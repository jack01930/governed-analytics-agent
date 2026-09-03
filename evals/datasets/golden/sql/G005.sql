-- G005 metric_version=1.0.0
with previous_week as (
  select c.segment as segment, sum(oi.net_amount) as gmv
  from customers as c
  join orders as o on o.customer_id = c.customer_id
  join order_items as oi on oi.order_id = o.order_id
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'
  group by c.segment
), current_week as (
  select c.segment as segment, sum(oi.net_amount) as gmv
  from customers as c
  join orders as o on o.customer_id = c.customer_id
  join order_items as oi on oi.order_id = o.order_id
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-08T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'
  group by c.segment
)
select coalesce(previous_week.segment, current_week.segment) as segment,
  coalesce(previous_week.gmv, 0) as previous_gmv,
  coalesce(current_week.gmv, 0) as current_gmv,
  coalesce(current_week.gmv, 0) - coalesce(previous_week.gmv, 0) as delta
from previous_week
full outer join current_week on current_week.segment = previous_week.segment
order by segment asc;
