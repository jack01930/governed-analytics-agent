# Text-to-SQL 与 Week 2 分层评测

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
计量见 [live 证据归档](reports/evidence/README.md)。Week 2 唯一一次获授权的 live 结果及失败模式见
[Week 2 DeepSeek live 分析](reports/week-2-deepseek-live-analysis-2026-09-04.md)。

## Week 3 Agent 分层评测

Week 3 使用独立的 `week3-agent-evaluation-v1` 协议，不把 Week 2 static safety 或裸 Text-to-SQL 分数混入
Agent 分母。fixture 固定运行 30 个 known 与 10 个 heldout 用例；live 路径排除 4 个仅用于确定性 fault/policy
harness 的 known 用例，因此固定为 36 例。正式报告绑定 overall、known、heldout 与按 mode/有序 case ID 派生的
executed manifest hash，subset 只能作为 `partial_test`，不能冒充正式报告。

指标全部使用自身适用分母：known behavior 10、known simple 15、known attribution 1，heldout 10 独立列示；
first/final 的 result、alias/output contract、production validation、execution、truncation、strict 分别报告。
此外独立报告 repair、valid execute、natural refusal、tool order、verified evidence 与 budget 的
`numerator/denominator/rate`；无适用项时 denominator 为 0、rate 为 N/A，绝不以总 40 例代替。

fixture executor 为每个 case 新建 scripted model session、预算、trace 与 context，但 40 例共享一个调用方拥有的
只读 PostgreSQL engine/tool registry。非 execute 行为用例无数据库调用；ProfileTool 会通过受控工具边界执行聚合
SQL。报告只保留受限 trace metadata、验证和 evidence
引用、计数、成本与安全终态，不包含 question、prompt、SQL、参数、raw rows、payload、endpoint、key 或价格来源。
fixture 的全绿只证明 harness、tools、数据库治理、预算与 scorer 的一致性，不证明模型质量或语言泛化。

```bash
make eval-week3-fixture
```

报告写入不可覆盖目录 `artifacts/evals/week3/fixture/<timestamp>-<run-id>/`；命令另将本次 runner 返回的精确
`report.json` 绝对路径写入 `artifacts/evals/week3/fixture-report-path.txt`，不搜索历史目录。live 命令已实现但
本计划未授权执行；它要求 tiny dataset、显式 `--live`、`AGENT_RUNTIME_MODE=live`、
`AGENT_LIVE_ENABLED=true`、有效模型设置/key 与 requested model 对应的公共价格快照全部通过后，才会创建
`AsyncOpenAI(max_retries=0)`：

```bash
uv run governed-eval week3 --dataset tiny --mode live --live
```

DeepSeek 当前可能把请求别名 `deepseek-v4-flash` 原样写入 `response.model`，而价格快照的 `resolved_model`
保存官方版本标签 `DeepSeek-V4-Flash-0731`。预算结算只接受价格快照绑定的这两个精确值，不接受任意 alias；
Week 3 的 `resolved_models` 保留 Provider 实际返回的安全模型名。陌生或同一轮混合模型身份会 fail closed，但仍
发布不通过的安全报告，避免因报告契约二次失败而丢失本轮结果。

CI 与 Makefile 只包含 fixture 命令，不携带 key、URL、network client 或 live 开关。任何 Week 3 live 都必须在
审阅本次离线报告后获得新的、单次明确授权。

## Week 2 活跃评测协议

Week 2 不修改 core-v1 或历史 live 报告，而是新增独立的 `week2-evaluation-v1` 协议：

| 套件 | 数量 | 目的 | 分母 |
| --- | ---: | --- | --- |
| core-v2 | 20 | 把 core-v1 的日期、窗口和业务对象改为每题自包含 | 业务结果与字段契约 |
| paraphrase | 20 | 检查同一意图的同义、口语和语序变化 | 业务结果与字段契约 |
| boundary | 10 | 检查半开时间窗、NULL、严格比较和稳定 top-k | 业务结果与字段契约 |
| safety | 20 | 检查写操作、多语句、系统表、危险函数、敏感输出等攻击 | 安全拒绝率 |

业务例共 50 条，分别报告 `result_accuracy`、`output_contract_rate`、`valid_sql_rate` 和
`execution_success_rate`；20 条 safety 不混入业务正确率。safety 只有实际拒绝码与预期拒绝码完全相同才算
通过。报告还记录规范化的 `finish_reason`、`output_truncated`、tokens、成本、延迟、数据集 ID、套件哈希、
实现哈希、查询指纹，以及定价快照哈希和请求/解析模型，但不保存生成 SQL、Provider 原始响应、端点或凭据。
实现哈希覆盖会影响评测的源码、迁移、治理配置和锁定依赖，使脱离 Git 的本地报告也能区分策略或评分实现变化。

离线全流程命令为：

```bash
make eval-week2-fixture
```

它执行 50 次 Oracle fixture 生成和 50 次只读数据库查询，并直接用策略验证 20 条安全 SQL；不读取
`MODEL_API_KEY`，不创建网络客户端。2026-09-04 的验收运行结果为 50/50 业务结果正确、50/50 字段契约
合规、20/20 安全规则匹配，tokens 与成本均为 0。这个 70/70 是 harness 验证，不能与 Week 1 DeepSeek
live 的 5/20 相减后宣称模型提升。

本次最终 fixture 的 run ID 为 `4f9f6872a315429da139accecc7a6771`，suite manifest SHA-256 为
`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`，implementation SHA-256 为
`e7c20ed70bd2754a5e4f41c06b4f5a35b3cd6447e1481c642075c0386a25d744`；它绑定的 Week 1 报告规范化内容
SHA-256 为 `91e24468a0d37601154a02b502868136f7352551b1059bbafbe7508451d3ef11`。成本状态为完整，未定价调用为 0。

## Week 2 live 结果与边界

2026-09-04 已按用户的单次付费授权冻结 suite hash、dataset、prompt/context、模型和价格，并且只执行一次：

```bash
uv run governed-eval week2 --dataset tiny --mode live --live
```

run ID `4afcbfcb01e5401caaea2d1b57e041db` 的 50 个业务题结果正确率为 74%、字段契约率 68%、有效 SQL 率与
执行成功率均为 94%，无截断，估算成本 CNY 0.644319490584。20 个 safety 例由确定性本地策略直接验证并
全部按预期拒绝，不调用模型，不能表述为模型安全能力。该流程仍是每题一次生成、一次策略校验、一次执行，
没有调用四类工具或 Agent 修复循环；它是第 3 周工具编排前的裸基线。

缺少 `--live`、非 tiny 数据集、空 Key、模型与价格快照不匹配时，CLI 都会在构造网络客户端之前失败。正式
运行在首个模型请求前独占报告 staging、检查最终目标并验证当前文件系统支持原子 no-replace；冲突、不可写
或原子能力缺失时直接停止。随后逐题校验 Provider 返回模型是否属于价格快照声明的请求/解析模型；无法绑定
价格的响应记为 `pricing_failed` 且不执行其 SQL，同时将成本完整性标为 false、禁止声明历史可比。运行仍是
一题一次生成、一次执行、无重试；失败题不得删除或挑选性重跑。现有单次授权已经用完，任何新 live 均需新的
明确付费授权。完整比较结论见 [Week 1 → Week 2 对比报告](reports/week-1-to-week-2-comparison-2026-09-04.md)。
