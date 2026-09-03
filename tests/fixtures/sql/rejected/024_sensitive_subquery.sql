select (
  select customer_code from customers order by customer_id asc limit 1
) as harmless_name
