select count(*) filter (where s.converted)::numeric
     / nullif(count(*), 0) as conversion_rate
from web_sessions as s
where s.occurred_at >= cast(:start_at as timestamptz)
  and s.occurred_at < cast(:end_at as timestamptz)
