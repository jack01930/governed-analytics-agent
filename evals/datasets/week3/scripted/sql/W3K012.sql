select coalesce(sum(p.amount), 0) as paid_gmv
from payments as p
where p.paid_at >= cast(:start_at as timestamptz)
  and p.paid_at < cast(:end_at as timestamptz)
  and p.status = 'succeeded'
