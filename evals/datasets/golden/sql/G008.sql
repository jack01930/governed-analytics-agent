-- G008 metric_version=1.0.0
with order_payments as (
  select p.order_id as order_id, sum(p.amount) as amount
  from payments as p
  join orders as o on o.order_id = p.order_id
  where p.status = 'succeeded'
    and o.ordered_at >= timestamptz '2026-05-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-01T00:00:00Z'
  group by p.order_id
), order_refunds as (
  select r.order_id as order_id, sum(r.amount) as amount
  from refunds as r
  join orders as o on o.order_id = r.order_id
  where r.status = 'succeeded'
    and o.ordered_at >= timestamptz '2026-05-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-01T00:00:00Z'
  group by r.order_id
)
select coalesce((select sum(order_payments.amount) from order_payments), 0)
  - coalesce((select sum(order_refunds.amount) from order_refunds), 0) as net_revenue;
