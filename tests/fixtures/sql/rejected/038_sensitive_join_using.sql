select count(*) as matches
from orders as left_order
join orders as right_order using (order_code);
