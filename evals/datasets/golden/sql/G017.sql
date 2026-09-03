-- G017 metric_version=1.0.0
with latest_run as (
  select pr.status as status, pr.watermark as watermark
  from pipeline_runs as pr
  where pr.pipeline_name = 'inventory'
    and pr.started_at >= timestamptz '2026-06-15T00:00:00Z'
    and pr.started_at < timestamptz '2026-06-16T00:00:00Z'
  order by pr.started_at desc, pr.pipeline_run_id desc
  limit 1
)
select coalesce((select latest_run.status is distinct from 'succeeded'
  or latest_run.watermark is null
  or latest_run.watermark < timestamptz '2026-06-15T23:59:59Z'
from latest_run), true) as is_stale;
