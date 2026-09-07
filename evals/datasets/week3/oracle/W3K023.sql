-- W3K023 fixed tiny-dataset Oracle
select count(*) filter (where s.converted)::numeric
     / nullif(count(*), 0) as conversion_rate
from web_sessions as s
where s.occurred_at >= timestamptz '2026-06-01T00:00:00Z'
  and s.occurred_at < timestamptz '2026-07-01T00:00:00Z'
