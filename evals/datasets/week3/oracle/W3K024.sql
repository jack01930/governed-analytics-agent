-- W3K024 fixed tiny-dataset Oracle
select count(*) filter (where i.available_qty = 0)::numeric
     / nullif(count(*), 0) as stockout_rate
from inventory_snapshots as i
where i.snapshot_at >= timestamptz '2026-06-01T00:00:00Z'
  and i.snapshot_at < timestamptz '2026-07-01T00:00:00Z'
