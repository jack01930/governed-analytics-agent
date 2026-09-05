-- W3K026 sku_contribution fixed tiny-dataset Oracle
select p.sku as sku,
  coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'), 0)
  - coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-08T00:00:00Z' and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'), 0) as gmv_loss
from orders as o
join order_items as oi on oi.order_id = o.order_id
join products as p on p.product_id = oi.product_id
where o.status in ('paid', 'completed', 'refunded')
  and ((o.ordered_at >= timestamptz '2026-06-01T00:00:00Z' and o.ordered_at < timestamptz '2026-06-08T00:00:00Z')
    or (o.ordered_at >= timestamptz '2026-06-08T00:00:00Z' and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'))
group by p.sku
order by gmv_loss desc, sku asc
fetch first 10 rows only
