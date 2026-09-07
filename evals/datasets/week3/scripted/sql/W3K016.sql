select count(*) filter (where p.status = 'succeeded')::numeric
     / nullif(count(*), 0) as payment_success_rate
from payments as p
where p.created_at >= cast(:start_at as timestamptz)
  and p.created_at < cast(:end_at as timestamptz)
