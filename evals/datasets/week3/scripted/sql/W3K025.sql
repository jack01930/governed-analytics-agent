with interval_attributions as (
  select a.campaign_id, a.attributed_revenue
  from campaign_attributions as a
  where a.attributed_at >= cast(:start_at as timestamptz)
    and a.attributed_at < cast(:end_at as timestamptz)
), campaign_spend as (
  select mc.campaign_id, mc.spend
  from marketing_campaigns as mc
  join (select distinct campaign_id from interval_attributions) as ia
    on ia.campaign_id = mc.campaign_id
)
select (coalesce((select sum(attributed_revenue) from interval_attributions), 0)
      - coalesce((select sum(spend) from campaign_spend), 0))
     / nullif((select sum(spend) from campaign_spend), 0) as campaign_roi
