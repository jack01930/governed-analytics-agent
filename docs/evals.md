# Text-to-SQL 基线评测

Week 1 使用 20 条版本化黄金业务问题（`G001` 到 `G020`）建立直接 Text-to-SQL 基线。每条问题有一条受控
Oracle 查询及其在 tiny 数据集上物化的只读结果；Oracle 是评测真值，不会发送给模型。模型上下文固定来自
迁移 `0001` 的 12 张公开分析表、字段/类型、主外键与必要枚举，以及固定顺序的 15 个治理指标元数据。上下文
版本和基于实际 UTF-8 上下文字节的 SHA-256 会并入 prompt version，且不含答案、Oracle SQL、异常清单、case ID、凭据或特权重置函数。

每个用例只允许一次生成、一次 SQL 执行、一次评分：没有重试、修复、工具循环或 LangGraph。Prompt v2
提供完整 JSON 示例并明确 `assumptions` 为字符串数组。SQL 先经过窄
只读 guard，再在 `analytics_readonly` 的只读、10 秒超时事务中执行。标量、表、top-k、布尔结果按列、键和
Decimal 容差评分。`passed` 表示分数 1；`wrong_answer` 表示已执行但答案不同；生成/guard 失败为
`invalid_sql`；数据库失败为 `execution_error`。输入、真值、上下文或发布失败是全局失败，不发布部分正式报告。

报告目录不可覆盖，包含 JSON、Markdown 和 20 个逐用例 JSON；汇总保留准确率、有效 SQL 率、执行成功率、
P50/P95 延迟、tokens 和估算 CNY 成本；内容解析失败也保留安全的模型名、耗时、tokens 和成本。live 报告还
记录价格生效日期与估算口径。JSON 为审计轨迹保留生成 SQL；异常原文、密钥与端点永不写入，Markdown
不嵌入 SQL。fixture 报告会明确标为
“评测链路验证，不代表模型质量”（报告原始固定标记：`Harness validation, not model quality.`）：`make eval-fixture` 的 100% 仅验证离线评测链路，live 结果才是模型
基线。

默认安全命令为：

```bash
make data-tiny
make data-verify
make metrics-check
make eval-fixture
```

它只运行 `governed-eval baseline --dataset tiny --mode fixture`，不构造网络客户端，也不需要模型 key。CI 同样
只能运行该 fixture 命令，绝不使用 `MODEL_API_KEY`、`--mode live` 或 `--live`。

live 路径已实现但不会由本项目自动执行。它必须同时显式授权：

```bash
uv run governed-eval baseline --dataset tiny --mode live --live
```

CLI 先拒绝 full，再检查 `--live`、非空 `MODEL_API_KEY`、设置和价格；全部通过后才会构造 OpenAI-compatible
客户端。默认 Base URL 为 `https://api.deepseek.com`，请求模型为 `deepseek-v4-flash`，官方版本快照记录为
`DeepSeek-V4-Flash-0731`。每题只调用一次 Chat Completions，设置 `temperature=0`、JSON Output、
`max_tokens=1200`，并通过 `thinking.type=disabled` 显式关闭思考模式。

DeepSeek 官方以美元计价且区分峰谷与缓存命中。为了不低估预算，版本化价格记录统一采用峰值时段、缓存未命中
上界：输入 USD 0.44/百万 tokens、输出 USD 1.32/百万 tokens，再按国家外汇管理局 2026-09-01 的
USD/CNY 6.7809 快照换算为输入 CNY 2.983596、输出 CNY 8.950788/百万 tokens。报告值是可复现的保守估算，
不等同于 DeepSeek 账户实际扣费；首次获授权的 live 报告即使表现不佳也必须保留。

接口与价格依据：[DeepSeek API 快速开始](https://api-docs.deepseek.com/)、
[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、
[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、
[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)和
[人民币汇率中间价](https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do)。

现有 `G001`–`G020` 冻结为未调优核心集。不要直接往同一准确率中混入安全拒绝和澄清行为；扩展分层及防污染
规则见 [Golden Questions 优化策略](golden-questions-strategy.md)。live 执行和结果到第 2 周任务的映射见
[DeepSeek live 基线运行手册](live-baseline-playbook.md)。2026-09-03 的 v1 适配器根因见
[第 1 周 DeepSeek 裸基线 v1 分析](reports/week-1-deepseek-live-analysis-2026-09-03.md)；v2 正式结果为 5/20、
有效 SQL 14/20、估算总成本 CNY 0.259588，逐题根因和第 2 周计划见
[v2 分析](reports/week-1-deepseek-live-v2-analysis-2026-09-03.md)。可克隆的脱敏原报告、SHA-256 与全部 case
计量见 [live 证据归档](reports/evidence/README.md)。
