-- G016 metric_version=1.0.0
with q2_campaigns as (
  select mc.campaign_id as campaign_id, mc.campaign_code as campaign_code, mc.spend as spend
  from marketing_campaigns as mc
  where mc.start_at < timestamptz '2026-07-01T00:00:00Z'
    and mc.end_at > timestamptz '2026-04-01T00:00:00Z'
), q2_revenue as (
  select ca.campaign_id as campaign_id, sum(ca.attributed_revenue) as attributed_revenue
  from campaign_attributions as ca
  where ca.attributed_at >= timestamptz '2026-04-01T00:00:00Z'
    and ca.attributed_at < timestamptz '2026-07-01T00:00:00Z'
  group by ca.campaign_id
)
select q2_campaigns.campaign_code as campaign_code,
  (coalesce(q2_revenue.attributed_revenue, 0) - q2_campaigns.spend)
    / nullif(q2_campaigns.spend, 0) as roi
from q2_campaigns
left join q2_revenue on q2_revenue.campaign_id = q2_campaigns.campaign_id
order by roi desc nulls last, campaign_code asc
limit 5;
