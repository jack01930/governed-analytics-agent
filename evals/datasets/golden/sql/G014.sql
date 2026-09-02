-- G014 metric_version=1.0.0
select count(*) as new_customers
from customers as c
where c.registered_at >= timestamptz '2026-06-01T00:00:00Z'
  and c.registered_at < timestamptz '2026-07-01T00:00:00Z';
