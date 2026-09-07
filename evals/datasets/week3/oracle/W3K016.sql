-- W3K016 fixed tiny-dataset Oracle
select count(*) filter (where p.status = 'succeeded')::numeric
     / nullif(count(*), 0) as payment_success_rate
from payments as p
where p.created_at >= timestamptz '2026-06-01T00:00:00Z'
  and p.created_at < timestamptz '2026-07-01T00:00:00Z'
