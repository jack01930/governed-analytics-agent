# 第 1 周 DeepSeek 裸基线分析

> 后续状态：本报告中的 P0 修复已完成，并在独立 Prompt v2 运行中验证。正式 v2 结果和第 2 周计划见
> [DeepSeek 裸基线 v2 分析](week-1-deepseek-live-v2-analysis-2026-09-03.md)。本 v1 报告保持不变作为历史证据。

## 运行身份

- 时间：2026-09-03（报告目录使用 UTC 时间）
- Run ID：`bf0e1dba448245e98988805103fd950d`
- 数据集 ID：`a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`
- 请求模型：`deepseek-v4-flash`，非思考模式
- Prompt：`baseline-system-v1+ecommerce-schema-v1+metrics-v1`
- 核心集：冻结的 `G001`–`G020`
- 运行边界：每题一次生成、一次执行机会、无重试、无修复、无工具循环

运行前 341 个单元测试、44 个集成测试、tiny 数据复现和 20/20 fixture 均通过。正式原始报告保存在
`artifacts/evals/baseline/live/20260903T095846Z-bf0e1dba448245e98988805103fd950d/`，未检测到
credential-like 文本。其逐字节脱敏副本与来源哈希已归档到
[`evidence/v1/`](evidence/v1/)，不依赖本地 ignored artifacts 即可审查。

## 原始结果

| 指标 | 结果 |
| --- | ---: |
| 结果准确率 | 0/20（0%） |
| 有效 SQL 率 | 0/20（0%） |
| 执行成功率 | 0/20（0%） |
| `generation_invalid_content` | 20/20 |
| 进入 SQL Guard | 0 |
| 进入 PostgreSQL | 0 |

这份 0% 不能解释为 DeepSeek 的 SQL 准确率为 0%。20 题都在 provider 响应到 SQL Guard 之间的适配器
解析阶段失败，尚未测量 SQL Guard、数据库执行或答案评分。

## 根因诊断

为避免记录原始模型文本，追加一次 G001 结构诊断，只输出类型和计量信息：

- provider 正常返回，`finish_reason=stop`；
- 返回内容是非空字符串，且可解析为 JSON object；
- JSON 恰好包含 `sql` 和 `assumptions` 两个键，没有额外字段；
- `sql` 是字符串，SQLGlot 能将其解析为一条 Query；
- `assumptions` 是字符串，而本地 Pydantic 契约要求字符串数组；
- 该诊断调用使用 3,393 input tokens、178 output tokens。

高置信根因是 Prompt v1 只要求两个键，没有明确说明 `assumptions` 必须是数组，也没有按照 DeepSeek JSON
Output 指南提供完整 JSON 示例。JSON mode 只保证合法 JSON，不保证本地 Pydantic Schema，因此所有回答在
辅助字段类型上被拒绝，正确或错误的 SQL 都失去了进入后续评分的机会。

## 观测与成本缺口

原始报告显示 token、延迟、估算成本均为 0，但 20 次 provider 调用确实已经发生。原因是当前 adapter 在内容
Schema 校验成功后才读取 usage；一旦内容无效，错误对象没有携带已发生调用的 latency/usage。G001 诊断调用按
项目的峰值缓存未命中上界估算约 CNY 0.0117，仅用于说明量级，不代表原 20 次调用的实际账单。

因此必须把“失败调用仍记录 usage/latency/cost”作为修复项，不能把原报告的 CNY 0 当作真实成本。

## 重跑前 P0 修复

1. 将 Prompt 升级为 v2，给出完整示例：`{"sql":"select ...","assumptions":["..."]}`，明确
   `assumptions` 是最多 8 项的字符串数组。
2. 不覆盖本次 v1 报告；v2 使用新的 prompt version，作为修复后的可比运行。
3. 让安全错误对象携带 latency、input/output tokens 和 provider model，但绝不携带原始响应、Key 或端点。
4. 把 `invalid_content` 细分为 JSON、响应字段、assumptions 类型和 SQL 形状等安全类别。
5. 将价格快照/估算口径写入 live 报告，明确报告成本与 provider 实际账单的差异。
6. 用 DeepSeek 字符串 assumptions、数组 assumptions、空内容和截断内容的伪响应补齐单元测试。

以上修复不增加重试，也不放宽 SQL Guard；它们只修正输入/观测契约，让裸基线真正测到 SQL。

## 对第 2 周工作的导向

当前证据要求先把“可测量性”放在完整安全工具层之前：

| 优先级 | 工作 | 验收 |
| ---: | --- | --- |
| P0 | DeepSeek 结构化输出契约 v2 | G001 伪响应和实际诊断均可进入 SQL Guard |
| P0 | 失败调用 telemetry 与成本核算 | 任意 provider 内容失败仍记录非零 latency/tokens/cost |
| P0 | 保留 v1、经再次授权运行一次 v2 | 得到可衡量 SQL/答案质量的第二份独立报告 |
| P1 | Schema Tool + Metric Context Tool | 若 metric/comparison 题错误，能定位到表字段或口径检索 |
| P1 | 完整 SQLGlot 策略与安全集 | DDL/DML/多语句/危险函数拦截率 100%，并统计合法 SQL 误杀率 |
| P1 | Profile Tool + Execute SQL Tool | 执行错误结构化，超时/行数/只读/脱敏可测试 |
| P2 | Golden paraphrase/boundary 扩展 | 等 v2 core 结果后按真实薄弱类别分配新增题量 |

在获得 v2 结果前，不应因为本次 0% 去修改 Oracle、删除难题或盲目增加 Agent 重试。v2 若主要出现
`wrong_answer`，再把优先级转向 Schema/指标上下文；若主要出现 `sql_rejected`，优先完整策略与误杀分析；
若复杂归因题集中失败，则把 Profile Tool 做好，并将多步规划留给第 3 周 LangGraph。
