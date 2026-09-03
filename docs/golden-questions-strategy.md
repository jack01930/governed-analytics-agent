# Golden Questions 优化策略

## 目标

现有 `G001`–`G020` 是第 1 周裸 Text-to-SQL 的 core v1。DeepSeek v1/v2 live 已在不修改题目、Oracle 或
评分规则的前提下完成，历史结果必须继续保持不可变。v2 实测发现 `G002`–`G006` 和 `G010` 依赖未提供的
前文，因此第 2 周新增了每题自包含的 core-v2，而没有回写 core v1 或删除难题。后续优化通过用途明确、
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

第 2 周已达到 70 个活跃例（core-v2 20 + paraphrase 20 + boundary 10 + safety 20），并保留 core-v1 历史集；澄清行为需要 Agent 状态和输出
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
10. 自包含不只意味着补日期：还要逐题写明时间字段、UTC 半开窗口、状态过滤、聚合字段、零值/NULL、
    缺侧补零、Top-K 数量及稳定排序；题面必须能唯一推导 Oracle，不能依赖评测者心中的隐含口径。

第 2 周收口时已对 C201–C220 与 P201–P220 逐条比对 G001–G020 Oracle。40 条业务题均通过上述语义闭合
复核；尤其修正了 C206 固定地区/SKU 归因范围，以及“最常见退款原因”与实际按退款金额排序之间的歧义。

## Week 1 v2 live 导出的第 2 周扩展

- paraphrase 20：指标 6 条、比较/贡献 8 条、异常/质量 6 条。
- boundary 10：时间边界 4 条、NULL/零分母 2 条、重复/退款 2 条、top-k 并列与稳定排序 2 条。
- safety 20：DDL/DML、多语句、系统/危险函数、非确定时间来源和超大结果各 4 条。
- Guard 评测同时包含合法只读样本，明确量出误杀率；`MAX`、`EXISTS` 是本次必须覆盖的回归构造。
- G006 类复杂归因题加入输出截断标签，并比较上下文裁剪与输出预算，不通过自动重试抬分。

正式证据、逐题根因和上述分配依据见
[DeepSeek 裸基线 v2 分析](reports/week-1-deepseek-live-v2-analysis-2026-09-03.md)。

## Week 2 live 导出的下一轮优化

唯一一次 Week 2 live 表明：core-v2、paraphrase、boundary 的结果正确率分别为 70%、85%、60%，字段契约率
分别为 85%、65%、40%。因此下一轮不扩充更多同质问题，而优先补强可解释性和行为层：

- 保留当前 70 例为冻结回归集，另建 held-out 改写，避免围绕已见失败题调参后再用同一集合宣称提升；
- 为缺日期、指标口径或权限的问题新增 `behavior/clarification`，独立评估追问、拒答和无法回答；
- 为复杂 top-k、退款率、NULL/零分母、严格不等式和稳定排序增加结构化约束标签与最小对照例；
- 每个意图保留 core/paraphrase 配对，报告语义一致率和严格通过一致率，不只报告总体平均；
- 输出契约另设诊断字段，只记录列数、行数、键差异和数值误差摘要，不保存生成 SQL 或实际业务值；
- 后续若获新的付费授权，对分歧题做预先登记的重复测量并报告波动，不选择性重跑。

本次报告不保存生成 SQL、实际结果或实际列名，因此只能把题目对之间的状态模式作为待验证假设，不能断言
具体 join、filter、时间窗或聚合根因。完整分析见
[Week 2 DeepSeek live 分析](reports/week-2-deepseek-live-analysis-2026-09-04.md)。
