select ctid::text, xmin::text, tableoid
from orders
limit 1;
