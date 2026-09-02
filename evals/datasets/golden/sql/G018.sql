-- G018 metric_version=1.0.0
with duplicate_groups as (
  select oi.source_line_id as source_line_id, count(*) - 1 as excess_rows
  from order_items as oi
  join orders as o on o.order_id = oi.order_id
  where o.ordered_at >= timestamptz '2026-04-10T00:00:00Z'
    and o.ordered_at < timestamptz '2026-04-11T00:00:00Z'
  group by oi.source_line_id
  having count(*) > 1
)
select coalesce(sum(duplicate_groups.excess_rows), 0) as duplicate_rows
from duplicate_groups;
