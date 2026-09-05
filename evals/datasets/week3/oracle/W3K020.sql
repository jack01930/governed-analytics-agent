-- W3K020 fixed tiny-dataset Oracle
select count(distinct c.customer_id) as new_customers
from customers as c
where c.registered_at >= timestamptz '2026-06-01T00:00:00Z'
  and c.registered_at < timestamptz '2026-07-01T00:00:00Z'
