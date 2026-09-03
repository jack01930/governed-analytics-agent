-- G013 metric_version=1.0.0
select count(distinct o.customer_id) as active_customers
from orders as o
where o.status in ('paid', 'completed', 'refunded')
  and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
  and o.ordered_at < timestamptz '2026-07-01T00:00:00Z';
