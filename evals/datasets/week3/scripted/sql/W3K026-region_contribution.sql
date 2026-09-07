select o.region as region,
  coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz)), 0)
  - coalesce(sum(oi.net_amount) filter (where o.ordered_at >= cast(:current_start as timestamptz) and o.ordered_at < cast(:current_end as timestamptz)), 0) as gmv_loss
from orders as o join order_items as oi on oi.order_id = o.order_id
where o.status in ('paid', 'completed', 'refunded')
  and ((o.ordered_at >= cast(:previous_start as timestamptz) and o.ordered_at < cast(:previous_end as timestamptz))
    or (o.ordered_at >= cast(:current_start as timestamptz) and o.ordered_at < cast(:current_end as timestamptz)))
group by o.region
order by gmv_loss desc, region asc
fetch first 10 rows only
