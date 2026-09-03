-- G001 metric_version=1.0.0
select coalesce(sum(oi.net_amount), 0) as gmv
from orders as o
join order_items as oi on oi.order_id = o.order_id
where o.status in ('paid', 'completed', 'refunded')
  and o.ordered_at >= timestamptz '2026-06-08T00:00:00Z'
  and o.ordered_at < timestamptz '2026-06-15T00:00:00Z';
