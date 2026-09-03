# Golden Questions 优化策略

## 目标

现有 `G001`–`G020` 是第 1 周裸 Text-to-SQL 的 core v1。DeepSeek v1/v2 live 已在不修改题目、Oracle 或
评分规则的前提下完成，历史结果必须继续保持不可变。v2 实测发现 `G002`–`G006` 和 `G010` 依赖未提供的
前文，因此第 2 周应新增每题自包含的 core-v2，而不是回写 core v1 或删除难题。后续优化通过用途明确、
可分别解释的套件完成。

## 分层评测结构

| 套件 | 第 2 周目标规模 | 目的 | 主要评分 |
| --- | ---: | --- | --- |
| `golden/core-v1` | 20（冻结） | 固定第 1 周历史结果与可比性 | 原评分规则，不再修改 |
| `golden/core-v2` | 20 | 自包含的单轮业务答案基线 | 结果正确率、字段契约率、有效 SQL 率 |
| `golden/paraphrase` | 20 | 同一意图的口语、同义和语序变化 | 与对应 core Oracle 一致、意图稳定性 |
| `golden/boundary` | 10 | 半开时间窗、零分母、并列 top-k、NULL、跨月等边界 | 精确结果与确定性排序 |
| `safety/adversarial` | 20 | DDL/DML、多语句、系统表、危险函数、注入和超大结果 | 拦截率、误杀率、数据库纵深防御 |
| `behavior/clarification` | 第 3 周增加 10 | 缺少时间/指标、越界问题、无法回答问题 | 澄清/拒答正确率，不以 SQL 准确率计分 |

第 2 周先达到 70 个活跃例（core-v2 20 + paraphrase 20 + boundary 10 + safety 20），另保留 core-v1 历史集；澄清行为需要 Agent 状态和输出
契约，放到第 3 周，避免用当前“必须生成 SQL”的裸基线错误评分。

## 用例元数据

扩展用例至少记录：

- `case_id`：套件内稳定 ID；
- `intent_id`：与 core 问题的意图关联；
- `suite`：core、paraphrase、boundary、safety 或 clarification；
- `difficulty`：easy、medium、hard；
- `risk_tags`：时间、口径、join、聚合、top-k、权限、注入等；
- `expected_behavior`：execute、reject 或 clarify；
- `oracle_sql_path` / `expected_error`：只允许二选一；
- `source` 和 `review_status`：记录来源与人工复核状态。

## 生成与审核原则

1. 每条 paraphrase 只改变表达，不改变业务条件；通过 `intent_id` 复用同一 Oracle。
2. 边界题必须由数据事实支持，不能为了“难”而引入无唯一答案的问题。
3. 安全题不进入业务准确率分母，分别报告攻击拦截率和正常 SQL 误杀率。
4. 澄清题明确列出缺失信息以及期望追问，不强迫模型猜测日期或指标口径。
5. Oracle SQL、异常 manifest、预期答案和 case ID 不发送给 live 模型。
6. 新题先由 SQL 执行和结果形状校验，再进行人工业务语义复核。
7. 首次 live 报告发布后只追加新版本；禁止覆盖报告、删除失败题或修改原题抬高分数。
8. 单轮 `execute` 问题必须自包含日期、比较窗口和业务对象；“该周”“上述异常”等承接表达只能进入
   conversation suite。
9. scalar/boolean 结果正确性按单格值评分，输出字段名另计 contract conformance；不得因别名不同把正确数值
   与错误数值混为一类。

## v2 live 导出的扩展重点

- paraphrase 20：指标 6 条、比较/贡献 8 条、异常/质量 6 条。
- boundary 10：时间边界 4 条、NULL/零分母 2 条、重复/退款 2 条、top-k 并列与稳定排序 2 条。
- safety 20：DDL/DML、多语句、系统/危险函数、非确定时间来源和超大结果各 4 条。
- Guard 评测同时包含合法只读样本，明确量出误杀率；`MAX`、`EXISTS` 是本次必须覆盖的回归构造。
- G006 类复杂归因题加入输出截断标签，并比较上下文裁剪与输出预算，不通过自动重试抬分。

正式证据、逐题根因和上述分配依据见
[DeepSeek 裸基线 v2 分析](reports/week-1-deepseek-live-v2-analysis-2026-09-03.md)。
