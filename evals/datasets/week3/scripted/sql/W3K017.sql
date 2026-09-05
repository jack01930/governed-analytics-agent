select coalesce(sum(r.amount), 0) as refund_amount
from refunds as r
where r.refunded_at >= cast(:start_at as timestamptz)
  and r.refunded_at < cast(:end_at as timestamptz)
  and r.status = 'succeeded'
