-- G020 metric_version=1.0.0
with day_refunds as (
  select r.order_id as order_id, sum(r.amount) as refund_amount
  from refunds as r
  where r.status = 'succeeded'
    and r.refunded_at >= timestamptz '2026-05-20T00:00:00Z'
    and r.refunded_at < timestamptz '2026-05-21T00:00:00Z'
  group by r.order_id
), successful_payments as (
  select p.order_id as order_id, sum(p.amount) as payment_amount
  from payments as p
  where p.status = 'succeeded'
  group by p.order_id
)
select count(*) as over_refunded_orders
from day_refunds
left join successful_payments on successful_payments.order_id = day_refunds.order_id
where day_refunds.refund_amount > coalesce(successful_payments.payment_amount, 0);
