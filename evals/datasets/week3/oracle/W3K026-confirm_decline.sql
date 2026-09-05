-- W3K026 confirm_decline fixed tiny-dataset Oracle
select
  coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-08T00:00:00Z' and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'), 0) as current_gmv,
  coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'), 0) as previous_gmv,
  (coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-08T00:00:00Z' and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'), 0)
   - coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'), 0))
  / nullif(coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'), 0), 0) as change_rate
from orders as o join order_items as oi on oi.order_id = o.order_id
where o.status in ('paid', 'completed', 'refunded')
