select count(distinct o.order_id) as valid_order_count
from orders as o
where o.ordered_at >= cast(:start_at as timestamptz)
  and o.ordered_at < cast(:end_at as timestamptz)
  and o.status in ('paid', 'completed', 'refunded')
