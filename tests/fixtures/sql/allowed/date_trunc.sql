select date_trunc('day', o.ordered_at) as ordered_day, count(*) as order_count
from orders as o
group by ordered_day
order by ordered_day asc
