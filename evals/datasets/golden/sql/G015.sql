-- G015 metric_version=1.0.0
with active_customers as (
  select distinct o.customer_id as customer_id
  from orders as o
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-07-01T00:00:00Z'
), lifetime_orders as (
  select o.customer_id as customer_id
  from orders as o
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at < timestamptz '2026-07-01T00:00:00Z'
  group by o.customer_id
  having count(distinct o.order_id) >= 2
)
select count(lifetime_orders.customer_id)::numeric / nullif(count(active_customers.customer_id), 0)
  as repeat_purchase_rate
from active_customers
left join lifetime_orders on lifetime_orders.customer_id = active_customers.customer_id;
