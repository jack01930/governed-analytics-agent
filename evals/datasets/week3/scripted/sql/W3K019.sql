select count(distinct o.customer_id) as active_customers
from orders as o
where o.ordered_at >= cast(:start_at as timestamptz)
  and o.ordered_at < cast(:end_at as timestamptz)
  and o.status in ('paid', 'completed', 'refunded')
