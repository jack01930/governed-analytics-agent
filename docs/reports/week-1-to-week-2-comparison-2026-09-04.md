# Week 1 → Week 2 对比报告

> 结论日期：2026-09-04
> 比较原则：正式历史分、评分审计值、离线 harness 验证和真实 live 结果严格分开。

## 结论

第 2 周工程目标和唯一一次获授权的 live 运行均已完成。评测从 20 条隐含上下文较多的 core-v1 扩展为
20 条 core-v2、20 条 paraphrase、10 条 boundary 和 20 条 safety；结果正确率与字段契约率已拆分，四类安全
工具、SQL 策略和数据库纵深防御通过工程门禁。

Week 2 live 在 50 条业务题上得到 37/50（74%）结果正确、34/50（68%）字段契约合规、47/50（94%）SQL
有效且执行成功；20 条 safety 全部按预期拒绝。该结果不能写成“模型从 25% 提升到 74%”：Week 1 的 25%
是旧协议正式分，按单格值重新审计为 8/20（40%）；Week 2 又改变了题面自包含性、评分和 SQL 策略，并扩展了
套件。报告中的 `comparable` 只表示元数据满足“业务意图对齐的协议比较”，明确不是同题准确率比较。

本次 live 仍是裸 Text-to-SQL：50 条业务题各一次模型生成、一次 SQL 执行，无重试、无 Agent 循环，也没有
调用已经实现的 Schema、Metric、Profile 或 Execute SQL Tool。20 条 safety 则由本地确定性策略直接校验，
不调用模型。因此，本次结果既不能归因于工具层，也不能把 safety 100% 当作模型安全能力。

## Week 1 双口径

- **正式历史口径：5/20（25%）**。这是不可覆盖的 Week 1 v2 报告结果，列名契约与数值正确性混在同一分数中。
- **评分审计口径：8/20（40%）**。`G008`、`G014`、`G018` 的单格值与 Oracle 一致，仅因旧评分先检查列名而
  未通过；该值是对同一次运行的审计，不是新 live，也不能替换正式 25%。
- Week 2 已把结果分和字段契约分开。若观察最接近的 20 组业务意图，应将 Week 1 审计 40% 与 core-v2 的
  14/20（70%）并列，差值为 30 个百分点；由于 core-v2 将题面改为自包含且策略也变化，这仍不是同题 A/B
  或纯模型能力增量。Week 2 全部 50 题的 74% 还包含 paraphrase 和 boundary，不能直接与 core-v1 相减。

## 三类结果

| 维度 | Week 1 DeepSeek v2 live | Week 2 fixture | Week 2 live |
| --- | ---: | ---: | ---: |
| 性质 | 真实模型单次裸跑 | 离线 Oracle/harness 验证 | 真实模型单次裸跑，无工具调用 |
| 业务题 | core-v1 20 | core-v2 20 + paraphrase 20 + boundary 10 | 同 fixture 的 50 题 |
| 结果正确 | 正式 5/20（25%）；审计 8/20（40%） | 50/50（100%） | 37/50（74%） |
| 完整通过 | 正式 5/20（25%） | 50/50（100%） | 30/50（60%） |
| SQL 有效并执行 | 14/20（70%） | 50/50（100%） | 47/50（94%） |
| 字段契约 | 历史协议未独立汇总 | 50/50（100%） | 34/50（68%） |
| 安全攻击 | 未作为独立套件 | 20/20 拒绝码匹配 | 20/20；本地策略直接验证，不调用模型 |
| 截断 | G006 达到输出上限，历史报告未单列 | 0 | 0 |
| 输入/输出 tokens | 68,525 / 6,160 | 0 / 0 | 175,211 / 13,581 |
| 估算成本 | CNY 0.259587769980 | CNY 0 | CNY 0.644319490584 |
| P50/P95 | 2,565 / 5,402 ms | 0 / 0 ms | 2,157 / 4,336 ms |
| 可比状态 | 冻结正式分；另保留 40% 审计值 | `not_comparable` | `comparable`，仅限 intent-aligned protocol comparison；非同题准确率 |

## Week 2 fixture 证据

- run ID：`4f9f6872a315429da139accecc7a6771`
- dataset ID：`a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`
- suite manifest SHA-256：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- implementation SHA-256：`e7c20ed70bd2754a5e4f41c06b4f5a35b3cd6447e1481c642075c0386a25d744`
- Week 1 reference canonical SHA-256：`91e24468a0d37601154a02b502868136f7352551b1059bbafbe7508451d3ef11`
- 套件结果：core-v2 20/20、paraphrase 20/20、boundary 10/10、safety 20/20
- 业务汇总：结果正确率 100%、字段契约率 100%、有效 SQL 率 100%、执行成功率 100%
- 成本元数据：`cost_estimate_complete=true`、`unpriced_call_count=0`，fixture 无定价快照、0 tokens、0 成本
- 隐私检查：正式 Week 2 报告不包含生成 SQL、模型 assumptions、Provider 原始响应、端点或 Key

运行时完整报告保存在被 Git 忽略的
`artifacts/evals/week2/fixture/20260903T175455Z-4f9f6872a315429da139accecc7a6771/`（目录时间为 UTC）；
本文件保留可版本化的关键元数据和结论。

## Week 2 live 证据

- 唯一正式 run ID：`4afcbfcb01e5401caaea2d1b57e041db`
- 运行目录：`artifacts/evals/week2/live/20260903T234213Z-4afcbfcb01e5401caaea2d1b57e041db/`（UTC）
- dataset ID：`a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`
- suite manifest SHA-256：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- implementation SHA-256：`e7c20ed70bd2754a5e4f41c06b4f5a35b3cd6447e1481c642075c0386a25d744`
- Week 1 reference canonical SHA-256：`91e24468a0d37601154a02b502868136f7352551b1059bbafbe7508451d3ef11`
- Prompt/context：`baseline-system-v2+ecommerce-schema-v1+metrics-v1+sha256:ca0f1d30dcb41c9c6634ac0bfb15e65ac2283ae4ccbfc11897b8e164dd681e25`
- 请求/响应模型：`deepseek-v4-flash`；定价版本：`DeepSeek-V4-Flash-0731`
- 定价快照 SHA-256：`465b1a9e1d37e2120884045b8cc3b820d7fa8455738ff204d1fa0fb1ac9e58e2`
- 价格口径：2026-09-01 峰值时段、缓存未命中上界、USD/CNY 6.7809；估算成本完整，未定价调用为 0
- 状态分布：`passed=30`、`contract_violation=7`、`invalid_sql=3`、`wrong_answer=4`、
  `wrong_answer_and_contract_violation=6`、`rejected=20`
- 运行约束：`one_generation_one_execution=true`，无重复挑分，模型生成截断为 0
- 原始汇总文件 SHA-256：`report.json` 为
  `18d607f8a99ab07bea35ed6e849ca052089ac6666a463f4b2bbc68fca2169db3`，`report.md` 为
  `4ffdd9e74b3b750cae2e6b2535da626c549f80ccdf6b7ef0f697b8ffd9c1ae31`

### 分套件结果

| 套件 | 业务/安全例 | 完整通过 | 结果正确 | 字段契约 | SQL 有效并执行 | 安全拒绝 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| core-v2 | 20 | 14/20（70%） | 14/20（70%） | 17/20（85%） | 18/20（90%） | — |
| paraphrase | 20 | 13/20（65%） | 17/20（85%） | 13/20（65%） | 19/20（95%） | — |
| boundary | 10 | 3/10（30%） | 6/10（60%） | 4/10（40%） | 10/10（100%） | — |
| safety | 20 | 20/20（100%） | — | — | — | 20/20（100%） |

`invalid_sql` 仅能从当前报告确认三个稳定类别：`C209` 为 `generation_invalid_assumptions`，`C212` 与
`P203` 为 `sql_rejected`。正式 Week 2 报告有意不保存生成 SQL、模型 assumptions 或 Provider 原始响应，
因此不能据此声称具体 SQL 根因；10 个错误结果和其他契约失败也只能作为后续受控诊断的样本入口。

## 从 Week 1 失败到 Week 2 改动

| Week 1 现象 | Week 2 改动 | 已验证结果 |
| --- | --- | --- |
| 6 题依赖“该周/上述”等未提供前文 | 新增逐题自包含 core-v2，core-v1 保持冻结 | registry 对 20 组意图与 Oracle 一一校验 |
| scalar/boolean 别名会污染结果分 | 结果正确率与字段契约率拆分 | scorer 与报告契约测试通过 |
| `MAX`、`EXISTS` 被窄 guard 误拒 | 分层 SQLGlot 正向函数策略 | 20 条 legacy Oracle 和合法语料零误拒 |
| DDL/DML、系统表、危险函数等需系统覆盖 | 44 条攻击 fixture + 20 条活跃 safety case | 全部拦截；安全例按精确拒绝码计分 |
| 整行复合值和身份元数据可绕过普通列名检查 | 按词法作用域拒绝本地/相关祖先整行引用，并封闭身份关键字 | 直接、类型转换、多层子查询均在建连前拒绝 |
| G006 截断只表现为无效 JSON | 适配层先提取并归一化 finish reason | 完整/无效 JSON 的 `length` 路径均有测试 |
| 后续 Agent 缺少小型可审计工具 | 新增 Schema、Metric、Profile、Execute SQL | 契约、参数绑定、只读事务和真实 DB 集成通过；本次裸 live 尚未调用这些工具 |
| 报告无法单独定位策略/评分代码版本 | 增加实现哈希，并记录定价快照的请求/解析模型 | fixture 报告可追溯；live 对未知 Provider 模型 fail-closed |
| 报告目标异常可能在付费调用后才暴露 | 首次生成前独占 staging、检查目标并验证原子 no-replace | 冲突、不可写或原子能力异常时模型调用数为 0 |

## 工程门禁

- Ruff：通过
- mypy：93 个源文件通过
- unit：672 项通过
- integration：66 项通过
- migration head：`0003`
- data verify、15 个指标、core-v1 fixture、Week 2 fixture：通过
- Git diff whitespace：通过

## 决策

1. Week 1 无需再改；core-v1 与 live v1/v2 继续作为冻结历史证据。
2. Week 2 工程和唯一一次 live 可以收口；run `4afcbfcb01e5401caaea2d1b57e041db` 作为不重跑、不挑分的
   裸流程基线。后续任何新增付费 live 仍需重新明确授权。
3. 第 3 周先打通 LangGraph 与 FastAPI 主链路，并把“裸流程”和“工具增强 Agent”作为不同协议分别报告。
   工具增强对照应冻结相同 dataset、suite、模型和价格，记录每个 Schema/Metric/Profile/Execute 调用，避免把
   题面、评分、策略或重试变化归因给工具。
4. P0 优先强化结构化输出与字段契约：boundary 契约率只有 40%，paraphrase 为 65%；同时保持结果正确率与
   契约率双指标，不能用自动改列名掩盖原始模型表现。
5. P0 对 10 个错误结果和 3 个 `invalid_sql` 建立受控诊断证据。当前脱敏报告没有 SQL 级根因材料，应通过
   不覆盖本次正式报告的本地回放或新增安全诊断字段定位后再修复，而不是凭状态码猜测。
6. safety 继续置于模型外的确定性策略层。20/20 证明本地拦截规则符合当前攻击集，不证明模型会自行拒绝攻击。
7. 将本次 P50/P95 和单次 50 题成本作为 Agent 路径预算基线；LangGraph 增加的模型轮次、重试、tokens、延迟
   和成本必须单独计量并设上限。

逐题配对、失败簇和 Week 3 验收建议见
[Week 2 DeepSeek live 裸流程分析](week-2-deepseek-live-analysis-2026-09-04.md)；可克隆的脱敏原报告见
[live 证据归档](evidence/README.md)。
