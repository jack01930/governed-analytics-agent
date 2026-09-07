with segment_gmv as (
  select c.segment as segment,
    coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz)), 0) as previous_gmv,
    coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:current_start as timestamptz) and o.ordered_at < cast(:current_end as timestamptz)), 0) as current_gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join customers as c on c.customer_id = o.customer_id
  where o.status in ('paid', 'completed', 'refunded')
    and ((o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz))
      or (o.ordered_at >= cast(:current_start as timestamptz) and o.ordered_at < cast(:current_end as timestamptz)))
  group by c.segment
)
select segment, previous_gmv, current_gmv, current_gmv - previous_gmv as delta
from segment_gmv
order by delta asc, segment asc
fetch first 10 rows only
