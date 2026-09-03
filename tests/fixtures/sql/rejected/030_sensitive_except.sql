select customer_code from customers
except
select customer_code from customers where false
