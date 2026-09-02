# 可复现模拟数据

生成器契约版本为 `1.1.0`，固定 seed 为 `20260901`，时间范围为
`[2025-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`。它只使用本地、命名隔离的
NumPy RNG（`named_rng(seed, namespace)`），不调用模型、网络或外部 API。

## 固定规模与表契约

| 表 | tiny | full | 规则 |
| --- | ---: | ---: | --- |
| categories | 20 | 20 | 固定品类 |
| customers | 500 | 50,000 | 配置 |
| products | 100 | 2,000 | 配置 |
| orders | 3,000 | 300,000 | 配置 |
| order_items | 实际记录于 manifest | 实际记录于 manifest | 每单 1–5 行；异常再复制 20 / 2,000 行 |
| payments | 3,000 | 300,000 | 每单一次尝试 |
| refunds | 实际记录于 manifest | 实际记录于 manifest | 成功退款基础概率 6% |
| inventory_snapshots | 54,500 | 1,090,000 | 546 天 × 商品数的基础数为 54,600 / 1,092,000；延迟异常删除 100 / 2,000 行 |
| web_sessions | 15,000 | 1,200,000 | tiny 为订单 5 倍，full 为 4 倍 |
| marketing_campaigns | 6 | 30 | 配置 |
| campaign_attributions | 实际记录于 manifest | 实际记录于 manifest | 从订单归因派生 |
| pipeline_runs | 1,638 | 1,638 | 546 天 × 三条管道 |

12 张表的固定载入/摘要顺序是 `categories`、`customers`、`products`、
`marketing_campaigns`、`orders`、`order_items`、`payments`、`refunds`、
`inventory_snapshots`、`web_sessions`、`campaign_attributions`、`pipeline_runs`。
金额在内存中为 integer cents/`Decimal`，CSV 始终以两位小数输出；时间统一 UTC；
空值为 CSV `\N`；数据库 identity 在每次载入后必须是连续 `1..N`。

## 八项可审计异常

| ID | UTC 窗口 / 配额 | 突变 | 可观察信号 |
| --- | --- | --- | --- |
| `anomaly_gmv_drop_south_conversion` | 2026-06-08 至 06-15 | 华南已转化会话按稳定 identity 反转 35%（等价目标转化概率 0.65） | 华南 GMV、已转化会话较前周下降 |
| `anomaly_gmv_drop_stockout` | 2026-06-08 至 06-15 | `SKU-000001/2` 可售量清零；按各自符合条件的群组稳定顺序取 floor(90%) 抑制订单行（tiny 各 18；full 为 1,803、1,804，合计 3,607，以清单为准） | 两个商品对 GMV 损失有实质贡献 |
| `anomaly_refund_spike_category` | 2026-05-04 至 05-11 | `CAT-018` 成功退款概率由 0.06 提至 0.24 | 品类退款率至少为 2 倍 |
| `anomaly_inventory_delay` | 2026-06-15 | 删除 UTC 08:00 后的库存快照，并写入失败的流水线运行记录 | 新鲜度 watermark 过期 |
| `anomaly_duplicate_order_items` | 2026-04-10；20 / 2,000 | 复制相同 `source_line_id`，新主键 | 逻辑重复数精确为配额 |
| `anomaly_order_amount_mismatch` | 2026-03-17；10 / 1,000 | `payable_amount` 增加 CNY 10.00 | 订单/明细对账失败数精确为配额 |
| `anomaly_missing_region` | 2026-02-12；10 / 1,000 | `orders.region = NULL` | 完整性空值数精确为配额 |
| `anomaly_refund_exceeds_payment` | 2026-05-20；3 / 300 | 成功退款较支付额额外增加 CNY 50.00 | 退款一致性违规数精确为配额 |

每条记录还携带半开区间、根因、稳定选择键哈希、突变参数、实际行数和预期信号；
以 `anomaly_manifest.json` 为唯一机器可读真值。

为使已声明的消费者问题可实际评分，生产器在不改变任一表固定规模的前提下，额外保留
上一周华南 `SKU-000001/2` 的有效订单基线 cohort（tiny 100 条、full 10,000 条），并将
至少五个不重叠营销活动排入 2026 Q2；每个 Q2 活动都有一个渠道和时间窗一致的归因订单。
这些 anchor 仅建立比较基线：华南 35% 的稳定转化翻转、每 SKU `floor(90%)` 缺货抑制和其余
八项异常的选择语义保持不变。

## 证据、载入与命令

`<output>/<scale>/` 下只生成本工具拥有的 CSV 与以下证据：

- `source_csv_digests.json`：规范源 CSV 字节、行数与 SHA-256；
- `anomaly_manifest.json`：异常真值；
- `dataset_manifest.json`：dataset/config/seed/scale 和按数据库 `COPY TO` 计算的 12 表摘要。

源摘要与数据库摘要刻意不同：前者来自生成的 CSV，后者来自数据库规范导出。
载入程序仅以 `analytics_loader` 最小权限角色连接，迁移 `0003` 的
`reset_analytics_dataset()` 在单一事务内清空并用 1 MiB chunk 的 PostgreSQL `COPY`
重新载入；不逐行 INSERT。只读指标使用 `analytics_readonly`。

```bash
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check

# full 仅在本地运行，绝不进入 CI
uv run governed-data generate --scale full --output artifacts/datasets
uv run governed-data verify --scale full --output artifacts/datasets
```

`generate` 固定读取仓库内 `data/generator/{tiny,full}.yaml`，只接受 scale 和
输出根目录；不会接受数据库、表或配置路径。`verify` 先严格读取既有预期
清单/异常/源证据，随后在同一输出文件系统的临时目录独立生成并
载入数据库，比较 dataset/config/seed/scale、固定顺序的 12 个表 count/DB SHA-256、
规范异常清单与源证据；它从不预先覆盖预期值，临时目录始终会清理。
安全再生的方式是明确执行 `generate` 覆盖该 scale 的已知 artifacts，绝不递归删除用户目录。

## 实测性能与本地证据

以下为 2026-09-02 本机实测（`/usr/bin/time -l`）。两次独立生成/load 的 12 个
`(table_name, row_count, database_sha256)` 均完全相等，anomaly/source manifests 也完全相等。

| 运行 | wall | 最大 RSS | 结果 |
| --- | ---: | ---: | --- |
| tiny generate | 4.81 s | 158,662,656 B | 通过（<60 s） |
| tiny verify | 2.25 s | 158,285,824 B | 通过 |
| full generate | 97.43 s | 1,407,582,208 B | 通过（<15 min） |
| full verify（独立生成/load） | 98.82 s | 1,391,132,672 B | 通过（<15 min） |

实测行数（顺序即 manifest/摘要顺序）为：tiny `20, 500, 100, 6, 3,000, 6,261,
3,000, 177, 54,500, 15,000, 117, 1,638`；full `20, 50,000, 2,000, 30, 300,000,
630,500, 300,000, 16,849, 1,090,000, 1,200,000, 42,709, 1,638`。完整 SHA-256
证据保留在本机 ignored 的 `artifacts/datasets/{tiny,full}/dataset_manifest.json`；所有
`artifacts/` 路径均由 `.gitignore` 忽略且未暂存。full 完成后已重新生成 tiny，开发数据库
当前恢复为 tiny。
