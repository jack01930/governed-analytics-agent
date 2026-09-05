with interval_attributions as (
  select distinct a.campaign_id, o.customer_id
  from campaign_attributions as a join orders as o on o.order_id = a.order_id
  where a.attributed_at >= cast(:start_at as timestamptz)
    and a.attributed_at < cast(:end_at as timestamptz)
), campaign_spend as (
  select mc.campaign_id, mc.spend
  from marketing_campaigns as mc
  join (select distinct campaign_id from interval_attributions) as ia
    on ia.campaign_id = mc.campaign_id
)
select coalesce((select sum(spend) from campaign_spend), 0)
     / nullif((select count(distinct customer_id) from interval_attributions), 0)
       as customer_acquisition_cost
