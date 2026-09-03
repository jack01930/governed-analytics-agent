-- G011 metric_version=1.0.0
select s.channel as channel,
  count(*) filter (where s.converted)::numeric / nullif(count(*), 0) as conversion_rate
from web_sessions as s
where s.occurred_at >= timestamptz '2026-06-08T00:00:00Z'
  and s.occurred_at < timestamptz '2026-06-15T00:00:00Z'
group by s.channel
order by channel asc;
