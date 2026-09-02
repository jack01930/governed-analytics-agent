-- G015 metric_version=1.0.0
with customer_orders as (
  select o.customer_id as customer_id, count(distinct o.order_id) as valid_order_count
  from orders as o
  where o.status in ('paid', 'completed', 'refunded')
    and o.ordered_at < timestamptz '2026-07-01T00:00:00Z'
  group by o.customer_id
)
select count(*) filter (where customer_orders.valid_order_count >= 2)::numeric
  / nullif(count(*), 0) as repeat_purchase_rate
from customer_orders;
