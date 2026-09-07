-- W3K018 fixed tiny-dataset Oracle
with order_payments as (
  select p.order_id, sum(p.amount) as amount
  from payments as p join orders as o on o.order_id = p.order_id
  where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-07-01T00:00:00Z' and p.status = 'succeeded'
  group by p.order_id
), order_refunds as (
  select r.order_id, sum(r.amount) as amount
  from refunds as r join orders as o on o.order_id = r.order_id
  where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-07-01T00:00:00Z' and r.status = 'succeeded'
  group by r.order_id
)
select coalesce((select sum(amount) from order_refunds), 0)
     / nullif((select sum(amount) from order_payments), 0) as refund_rate
