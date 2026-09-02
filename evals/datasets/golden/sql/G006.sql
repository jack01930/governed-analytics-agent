-- G006 metric_version=1.0.0
with south_conversion as (
  select
    count(*) filter (where s.occurred_at >= timestamptz '2026-06-08T00:00:00Z'
      and s.occurred_at < timestamptz '2026-06-15T00:00:00Z' and s.converted)::numeric
      / nullif(count(*) filter (where s.occurred_at >= timestamptz '2026-06-08T00:00:00Z'
        and s.occurred_at < timestamptz '2026-06-15T00:00:00Z'), 0) as current_value,
    count(*) filter (where s.occurred_at >= timestamptz '2026-06-01T00:00:00Z'
      and s.occurred_at < timestamptz '2026-06-08T00:00:00Z' and s.converted)::numeric
      / nullif(count(*) filter (where s.occurred_at >= timestamptz '2026-06-01T00:00:00Z'
        and s.occurred_at < timestamptz '2026-06-08T00:00:00Z'), 0) as previous_value
  from web_sessions as s
  join customers as c on c.customer_id = s.customer_id
  where c.region in ('东莞', '佛山', '南宁', '厦门', '广州', '海口', '深圳', '福州')
    and s.occurred_at >= timestamptz '2026-06-01T00:00:00Z'
    and s.occurred_at < timestamptz '2026-06-15T00:00:00Z'
), sku_gmv as (
  select p.sku as sku,
    coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
      and o.ordered_at < timestamptz '2026-06-08T00:00:00Z'), 0) as previous_value,
    coalesce(sum(oi.net_amount) filter (where o.ordered_at >= timestamptz '2026-06-08T00:00:00Z'
      and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'), 0) as current_value
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join products as p on p.product_id = oi.product_id
  where o.status in ('paid', 'completed', 'refunded')
    and p.sku in ('SKU-000001', 'SKU-000002')
    and o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'
    and o.ordered_at < timestamptz '2026-06-15T00:00:00Z'
  group by p.sku
), evidence as (
  select 'south_conversion'::text as cause_type, previous_value as previous,
    current_value as current, current_value - previous_value as delta
  from south_conversion
  union all
  select sku as cause_type, previous_value as previous, current_value as current,
    current_value - previous_value as delta
  from sku_gmv
)
select cause_type as cause_type, previous as previous, current as current, delta as delta
from evidence
order by case cause_type when 'south_conversion' then 1 else 2 end, cause_type asc;
