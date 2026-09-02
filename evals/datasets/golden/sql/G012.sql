-- G012 metric_version=1.0.0
select p.sku as sku, count(distinct i.snapshot_at::date) as stockout_days
from inventory_snapshots as i
join products as p on p.product_id = i.product_id
where i.available_qty = 0
  and i.snapshot_at >= timestamptz '2026-06-08T00:00:00Z'
  and i.snapshot_at < timestamptz '2026-06-15T00:00:00Z'
group by p.sku
order by stockout_days desc, sku asc;
