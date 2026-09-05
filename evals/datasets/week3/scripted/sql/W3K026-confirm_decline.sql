select
  coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:current_start as timestamptz) and o.ordered_at < cast(:current_end as timestamptz)), 0) as current_gmv,
  coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz)), 0) as previous_gmv,
  (coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:current_start as timestamptz) and o.ordered_at < cast(:current_end as timestamptz)), 0)
   - coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz)), 0))
  / nullif(coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz)), 0), 0) as change_rate
from orders as o join order_items as oi on oi.order_id = o.order_id
where o.status in ('paid', 'completed', 'refunded')
