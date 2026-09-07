-- W3K026 segment_contribution fixed tiny-dataset Oracle
with segment_gmv as (
  select c.segment as segment,
    coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'), 0) as previous_gmv,
    coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-08T00:00:00Z' and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'), 0) as current_gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join customers as c on c.customer_id = o.customer_id
  where o.status in ('paid', 'completed', 'refunded')
    and ((o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z')
      or (o.ordered_at >= timestamptz '2026-06-08T00:00:00Z' and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'))
  group by c.segment
)
select segment, previous_gmv, current_gmv, current_gmv - previous_gmv as delta
from segment_gmv
order by delta asc, segment asc
fetch first 10 rows only
