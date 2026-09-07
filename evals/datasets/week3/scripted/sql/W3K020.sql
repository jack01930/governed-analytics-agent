select count(distinct c.customer_id) as new_customers
from customers as c
where c.registered_at >= cast(:start_at as timestamptz)
  and c.registered_at < cast(:end_at as timestamptz)
