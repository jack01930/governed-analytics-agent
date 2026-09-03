select max(o.ordered_at) as latest_order_at, min(o.ordered_at) as earliest_order_at,
  avg(o.payable_amount) as average_payable
from orders as o
