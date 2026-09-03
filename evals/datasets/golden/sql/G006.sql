-- G006 metric_version=1.0.0
with cause_keys as (
  select cause_type as cause_type, sort_order as sort_order
  from (values ('south_conversion', 1), ('SKU-000001', 2), ('SKU-000002', 3))
    as causes(cause_type, sort_order)
), south_conversion as (
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
)
select cause_keys.cause_type as cause_type,
  case when cause_keys.cause_type = 'south_conversion'
    then coalesce(south_conversion.previous_value, 0)
    else coalesce(sku_gmv.previous_value, 0) end as previous,
  case when cause_keys.cause_type = 'south_conversion'
    then coalesce(south_conversion.current_value, 0)
    else coalesce(sku_gmv.current_value, 0) end as current,
  case when cause_keys.cause_type = 'south_conversion'
    then coalesce(south_conversion.current_value, 0) - coalesce(south_conversion.previous_value, 0)
    else coalesce(sku_gmv.current_value, 0) - coalesce(sku_gmv.previous_value, 0) end as delta
from cause_keys
left join south_conversion on cause_keys.cause_type = 'south_conversion'
left join sku_gmv on sku_gmv.sku = cause_keys.cause_type
order by cause_keys.sort_order asc, cause_keys.cause_type asc;
