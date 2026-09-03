select case when customer_id > 0 then customer_code else 'unknown' end as customer_label
from customers
