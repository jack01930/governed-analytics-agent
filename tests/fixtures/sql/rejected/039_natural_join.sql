select count(*) as matches
from orders as left_order
natural join orders as right_order;
