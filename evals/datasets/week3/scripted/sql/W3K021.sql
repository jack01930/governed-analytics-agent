with active_customers as (
  select distinct o.customer_id
  from orders as o
  where o.ordered_at >= cast(:start_at as timestamptz)
    and o.ordered_at < cast(:end_at as timestamptz)
    and o.status in ('paid', 'completed', 'refunded')
), lifetime_orders as (
  select o.customer_id
  from orders as o
  where o.ordered_at < cast(:end_at as timestamptz)
    and o.status in ('paid', 'completed', 'refunded')
  group by o.customer_id
  having count(distinct o.order_id) >= 2
)
select count(lo.customer_id)::numeric
     / nullif(count(ac.customer_id), 0) as repeat_purchase_rate
from active_customers as ac left join lifetime_orders as lo on lo.customer_id = ac.customer_id
