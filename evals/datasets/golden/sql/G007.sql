-- G007 metric_version=1.0.0
select coalesce(sum(p.amount), 0) as paid_gmv
from payments as p
where p.status = 'succeeded'
  and p.paid_at >= timestamptz '2026-06-01T00:00:00Z'
  and p.paid_at < timestamptz '2026-07-01T00:00:00Z';
