select coalesce(sum(oi.net_amount), 0) as also_wrong
from orders as o join order_items as oi on oi.order_id = o.order_id
where o.ordered_at >= cast(:start_at as timestamptz)
  and o.ordered_at < cast(:end_at as timestamptz)
  and o.status in ('paid', 'completed', 'refunded')
