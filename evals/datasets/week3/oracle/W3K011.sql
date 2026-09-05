-- W3K011 fixed tiny-dataset Oracle
select coalesce(sum(oi.net_amount), 0) as gmv
from orders as o join order_items as oi on oi.order_id = o.order_id
where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
  and o.ordered_at < timestamptz '2026-07-01T00:00:00Z'
  and o.status in ('paid', 'completed', 'refunded')
