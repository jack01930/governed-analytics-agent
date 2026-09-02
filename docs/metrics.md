# 首批 15 个权威指标

权威定义仅来自 [`data/metrics/core.yaml`](../data/metrics/core.yaml)，所有版本均为
`1.0.0`、有效期自 `2025-01-01T00:00:00Z` 起。没有 `session_count`；获客成本是
`customer_acquisition_cost`。下表的“公式”与 YAML 的 `expression_sql` 对应，时间字段
决定窗口归属，维度是未来查询规划器可追加的分组边界。

| ID | 公式 | 时间 | 单位 | 维度 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `gmv` | `sum(oi.net_amount)`，有效订单 | `o.ordered_at` | cny | region, channel, category, product, segment | orders, order_items, products, categories, customers |
| `paid_gmv` | `sum(p.amount)`，成功支付 | `p.paid_at` | cny | region, channel, segment | payments, orders, customers |
| `net_revenue` | 成功支付订单预聚合 − 成功退款订单预聚合 | `o.ordered_at` | cny | region, channel, category, product | orders, payments, refunds, order_items, products, categories |
| `valid_order_count` | `count(distinct o.order_id)`，有效订单 | `o.ordered_at` | count | region, channel, segment | orders, customers |
| `average_order_value` | `sum(oi.net_amount)/nullif(count(distinct o.order_id),0)` | `o.ordered_at` | cny | region, channel, segment | orders, order_items, customers |
| `payment_success_rate` | `succeeded payments / all payments` | `p.created_at` | ratio | provider, channel | payments, orders |
| `refund_amount` | `sum(r.amount)`，成功退款 | `r.refunded_at` | cny | reason, category, product, region | refunds, orders, order_items, products, categories |
| `refund_rate` | 成功退款订单预聚合 / 成功支付订单预聚合 | `o.ordered_at` | ratio | category, product, region | orders, payments, refunds, order_items, products, categories |
| `active_customers` | 窗口有效订单的 `count(distinct customer_id)` | `o.ordered_at` | count | region, segment | orders, customers |
| `new_customers` | `count(distinct c.customer_id)` | `c.registered_at` | count | region, segment | customers |
| `repeat_purchase_rate` | 窗口活跃客户中，截至窗口末累计 ≥2 有效单的比例 | `o.ordered_at` | ratio | region, segment | orders, customers |
| `customer_acquisition_cost` | 窗口参与活动的每活动一次 spend / 去重归因客户 | `a.attributed_at` | cny | campaign, channel | marketing_campaigns, campaign_attributions, orders |
| `conversion_rate` | `converted sessions / all sessions` | `s.occurred_at` | ratio | channel, region | web_sessions, customers |
| `stockout_rate` | `available_qty=0 snapshots / all snapshots` | `i.snapshot_at` | ratio | category, product | inventory_snapshots, products, categories |
| `campaign_roi` | `(归因收入预聚合 − 每活动一次 spend) / spend` | `a.attributed_at` | ratio | campaign, channel | marketing_campaigns, campaign_attributions |

五项标量 CTE/预聚合指标为 `net_revenue`、`refund_rate`、`repeat_purchase_rate`、
`customer_acquisition_cost`、`campaign_roi`。它们的完整、解析受限 SQL 在 core YAML 中；
前两项先按订单聚合付款/退款，后两项每活动只计一次 spend，复购的分子按窗口结束时的
累计订单计算。它们目前只定义标量语义；表中维度声明是未来规划器扩展分组时必须保持的
边界，不能把明细 join 直接塞进标量 CTE 而放大金额。

比例均以 0–1 表示，分母为零返回 `NULL`；`campaign_roi` 可为负或大于 1（仅 zero spend
为 `NULL`），CAC 的归因客户分母为零也为 `NULL`。

指标集成检查在显式 transaction 中执行 `SET TRANSACTION READ ONLY`、10 秒
`statement_timeout`、`search_path=public,pg_catalog` 和 UTC 时区：

```bash
make data-tiny
make metrics-check
```
