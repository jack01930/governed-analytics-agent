-- G009 metric_version=1.0.0
with category_orders as (
  select distinct c.category_code as category_code, o.order_id as order_id
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join products as p on p.product_id = oi.product_id
  join categories as c on c.category_id = p.category_id
  where o.ordered_at >= timestamptz '2026-05-04T00:00:00Z'
    and o.ordered_at < timestamptz '2026-05-11T00:00:00Z'
), successful_payments as (
  select p.order_id as order_id, sum(p.amount) as amount
  from payments as p
  where p.status = 'succeeded'
  group by p.order_id
), category_payments as (
  select category_orders.category_code as category_code,
    sum(successful_payments.amount) as payment_amount
  from category_orders
  join successful_payments on successful_payments.order_id = category_orders.order_id
  group by category_orders.category_code
), category_refunds as (
  select c.category_code as category_code, sum(r.amount) as refund_amount
  from refunds as r
  join orders as o on o.order_id = r.order_id
  join order_items as oi on oi.order_item_id = r.order_item_id
  join products as p on p.product_id = oi.product_id
  join categories as c on c.category_id = p.category_id
  where r.status = 'succeeded'
    and o.ordered_at >= timestamptz '2026-05-04T00:00:00Z'
    and o.ordered_at < timestamptz '2026-05-11T00:00:00Z'
  group by c.category_code
)
select coalesce(category_payments.category_code, category_refunds.category_code) as category_code,
  coalesce(category_refunds.refund_amount, 0)
    / nullif(category_payments.payment_amount, 0) as refund_rate
from category_payments
full outer join category_refunds
  on category_refunds.category_code = category_payments.category_code
order by refund_rate desc nulls last, category_code asc
limit 5;
