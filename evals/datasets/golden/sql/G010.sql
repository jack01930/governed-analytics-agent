-- G010 metric_version=1.0.0
select r.reason as reason, count(*) as refund_count, sum(r.amount) as refund_amount
from refunds as r
where r.status = 'succeeded'
  and r.refunded_at >= timestamptz '2026-05-04T00:00:00Z'
  and r.refunded_at < timestamptz '2026-05-11T00:00:00Z'
group by r.reason
order by refund_amount desc, reason asc
limit 5;
