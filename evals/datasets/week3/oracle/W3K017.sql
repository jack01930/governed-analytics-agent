-- W3K017 fixed tiny-dataset Oracle
select coalesce(sum(r.amount), 0) as refund_amount
from refunds as r
where r.refunded_at >= timestamptz '2026-06-01T00:00:00Z'
  and r.refunded_at < timestamptz '2026-07-01T00:00:00Z'
  and r.status = 'succeeded'
