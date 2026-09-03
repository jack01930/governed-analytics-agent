# Week 1 → Week 2 对比报告

> 结论日期：2026-09-04
> 比较原则：真实模型质量、离线 harness 验证和未运行项严格分开。

## 结论

第 2 周工程目标已经达到：评测从 20 条隐含上下文较多的 core-v1 扩展为 70 条分层用例，结果正确率与字段
契约率已拆分，SQL 策略不再误拒 `MAX` / `EXISTS` 等合法分析，四类安全工具和数据库纵深防御通过测试。

目前不能声称 DeepSeek 模型质量从 25% 提升到 100%。25% 是 Week 1 的真实 live 结果；100% 是 Week 2
Oracle fixture 对 harness 的自检。Week 2 live 尚未获得新的明确授权，也没有发起模型调用。

## 三类结果

| 维度 | Week 1 DeepSeek v2 live | Week 2 fixture | Week 2 live |
| --- | ---: | ---: | ---: |
| 性质 | 真实模型单次裸跑 | 离线 Oracle/harness 验证 | 尚未运行 |
| 业务题 | core-v1 20 | core-v2 20 + paraphrase 20 + boundary 10 | 计划同 fixture 的 50 题 |
| 结果正确 | 5/20（25%） | 50/50（100%） | 待测 |
| SQL 有效并执行 | 14/20（70%） | 50/50（100%） | 待测 |
| 字段契约 | 历史协议未独立汇总 | 50/50（100%） | 待测 |
| 安全攻击 | 未作为独立套件 | 20/20 拒绝码匹配 | 由确定性策略直接验证，不调用模型 |
| 截断 | G006 达到输出上限，历史报告未单列 | 0 | 待测，现已单列 `finish_reason` / `output_truncated` |
| 输入/输出 tokens | 68,525 / 6,160 | 0 / 0 | 待测 |
| 估算成本 | CNY 0.259587769980 | CNY 0 | 待测 |
| P50/P95 | 2,565 / 5,402 ms | 0 / 0 ms | 待测 |
| 可比状态 | 冻结历史基准 | `not_comparable` | 运行后按冻结元数据判断 |

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

## 从 Week 1 失败到 Week 2 改动

| Week 1 现象 | Week 2 改动 | 已验证结果 |
| --- | --- | --- |
| 6 题依赖“该周/上述”等未提供前文 | 新增逐题自包含 core-v2，core-v1 保持冻结 | registry 对 20 组意图与 Oracle 一一校验 |
| scalar/boolean 别名会污染结果分 | 结果正确率与字段契约率拆分 | scorer 与报告契约测试通过 |
| `MAX`、`EXISTS` 被窄 guard 误拒 | 分层 SQLGlot 正向函数策略 | 20 条 legacy Oracle 和合法语料零误拒 |
| DDL/DML、系统表、危险函数等需系统覆盖 | 44 条攻击 fixture + 20 条活跃 safety case | 全部拦截；安全例按精确拒绝码计分 |
| 整行复合值和身份元数据可绕过普通列名检查 | 按词法作用域拒绝本地/相关祖先整行引用，并封闭身份关键字 | 直接、类型转换、多层子查询均在建连前拒绝 |
| G006 截断只表现为无效 JSON | 适配层先提取并归一化 finish reason | 完整/无效 JSON 的 `length` 路径均有测试 |
| 后续 Agent 缺少小型可审计工具 | 新增 Schema、Metric、Profile、Execute SQL | 契约、参数绑定、只读事务和真实 DB 集成通过 |
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
2. Week 2 可以工程收口，后续可进入第 3 周 LangGraph 与 FastAPI 主链路。
3. 若要得到真实的 Week 1 → Week 2 模型效果变化，需用户重新明确授权一次 Week 2 live；运行前冻结当前
   dataset、suite hash、prompt/context、模型和价格，运行后保留全部失败题且不重复挑分。
