select exists(
  select 1 from orders as o where o.status = 'completed'
) as has_completed_order
