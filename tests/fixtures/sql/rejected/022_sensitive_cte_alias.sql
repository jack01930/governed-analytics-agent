with leaked_values as (
  select customer_code as harmless_name from customers
) select harmless_name from leaked_values
