select count(*) filter (where i.available_qty = 0)::numeric
     / nullif(count(*), 0) as stockout_rate
from inventory_snapshots as i
where i.snapshot_at >= cast(:start_at as timestamptz)
  and i.snapshot_at < cast(:end_at as timestamptz)
