# 第 2 周实施计划：评测有效性与安全工具层

## 状态

- 开始日期：2026-09-04
- 工作分支：`codex/week-2-safe-tools`
- 基线提交：`d58d0d5`（第 1 周收口合并）
- 完成日期：2026-09-04
- 当前状态：已完成（Week 2 live 待新的明确授权，不属于本次离线验收）

## 目标

在不引入 Agent 循环的前提下，完成可复用、可审计、可测试的安全数据工具层；同时修正第 1 周 live 暴露的评测失真，使后续模型或 Agent 改进能被可信地比较。

## 第 1 周比较基准

冻结下列历史证据，不修改题目、Oracle、评分或报告：

- core-v1：`G001`–`G020`
- DeepSeek v2 run ID：`65c87bf0355047a9b23243f70756e3f4`
- 结果正确：5/20（25%）
- SQL 有效并执行：14/20（70%）
- SQL 拒绝：5/20；无效 JSON：1/20
- 输入/输出 tokens：68,525 / 6,160
- P50/P95：2,565 / 5,402 ms
- 估算成本：CNY 0.259587769980

core-v2 会把隐含上下文改为自包含题面，因此只做“同一业务意图、不同测量协议”的并列比较，不把分数差直接宣称为模型能力提升。

## 范围

### P0：评测有效性

1. 保持 legacy `run_baseline()`、`GoldenCase`、core-v1 报告和 CLI 行为不变。
2. 新增 20 条自包含 core-v2，与 `G001`–`G020` 一一映射。
3. 新增 20 条 paraphrase、10 条 boundary、20 条 safety；活跃集共 70 条。
4. 结果正确率与输出字段契约率分开评分和报告。
5. 从模型适配层安全归一化 `finish_reason`，显式记录 `output_truncated`。

### P0：安全执行边界

1. 新增分层 SQLGlot 策略：解析、只读语句、关系、函数、确定性、输出/DLP、资源上限。
2. 允许 `MAX`、`MIN`、`AVG`、`EXISTS`、固定粒度 `date_trunc` 等合法分析表达式。
3. 拒绝 DDL/DML、多语句、系统/未登记关系、危险函数、非确定时间、`SELECT *`、敏感原始字段和不可审计 lineage。
4. 所有可执行 SQL 使用 `analytics_readonly`，事务固定为 `REPEATABLE READ, READ ONLY`，statement timeout 10 秒，UTC，固定 search path，最多返回 500 行。

### P1：四类工具

1. Schema Tool：只返回静态登记的业务表、字段和关系。
2. Metric Tool：通过稳定 ID 返回指标口径，不猜测未知指标。
3. Profile Tool：只接受结构化 profile 请求，值枚举最多 50；敏感列不得枚举原始值。
4. Execute SQL Tool：唯一自由只读 SQL 入口；先策略检查，再只读执行，再应用输出策略；错误只返回稳定分类。

### 明确不做

- 不实现 LangGraph、多轮规划、自动重试或自动修复；这些属于第 3 周。
- 不覆盖或重算第 1 周 live 报告。
- 不通过重复 live 运行挑选最好成绩。
- 未获得新的付费调用授权前，不发起第二周 live 调用。
- 不发布、不 push、不修改远程仓库可见性。

## 实施顺序

### 阶段 A：冻结契约与数据集

- 建立严格 suite registry，校验数量、ID、意图映射、review 状态、Oracle 引用和安全预期。
- core-v2 题面不得出现未解析的“该周”“上述”“这个异常”等指代。
- paraphrase 复用 core-v2 Oracle；safety 不进入业务正确率分母。
- boundary Oracle 在 tiny 数据集上物化并做问题—SQL—结果形状复核。

### 阶段 B：评分与遥测

- 新评分返回 `result_score` 与 `output_contract_conformant`。
- scalar/boolean 的值正确不再因列别名不同被判为结果错误。
- table/top-k 按 Oracle 列位置比较结果，列名完全匹配单独计为契约合规。
- `finish_reason` 只允许封闭枚举；仅 `length` 映射为 `output_truncated=true`。
- 无效 JSON 仍保留安全的 model、token、耗时、成本和截断信息，不保存原始响应。

### 阶段 C：安全策略与工具

- 先用全部 core-v1 Oracle 和合法 SQL 语料证明误拒率为 0%。
- 再用不少于 20 条攻击 SQL 证明拦截率为 100%，且拒绝发生在建连前。
- 实现 Schema、Metric、Profile、Execute SQL 四类稳定接口。
- 集成测试验证数据库角色、隔离级别、超时、行数、时区、search path 和敏感输出边界。

### 阶段 D：Week 2 runner 与报告

- 新增 Week 2 fixture/live runner、独立报告模型和不可覆盖的原子发布目录。
- fixture 模式执行 50 个业务例，直接校验 20 个 safety 例；不建立模型网络客户端。
- 分套件报告 result accuracy、contract rate、SQL/执行成功率、截断、tokens、成本和延迟。
- 报告带 suite manifest hash、implementation hash、dataset ID、prompt/context 版本、定价模型绑定和第 1 周比较引用。

### 阶段 E：回归、对比与收口

- legacy core-v1 fixture 必须继续 20/20，历史证据哈希不变。
- Week 2 fixture harness 必须 70/70；该分数只证明编排和 Oracle，不代表 live 模型质量。
- 生成一份 Week 1 → Week 2 对比报告，清楚区分“已实测 live”“fixture 验证”“尚未运行 live”。
- 更新 README、评测、安全与总计划文档后提交本地分支。

## 验收门禁

| 维度 | 门禁 |
| --- | --- |
| 历史兼容 | core-v1 20/20 fixture；归档 evidence 哈希不变 |
| 活跃评测 | 20 core-v2 + 20 paraphrase + 10 boundary + 20 safety，精确 70 例 |
| 评测语义 | 结果正确率与字段契约率独立；safety 不进入业务分母 |
| 安全 | 攻击集拦截 100%，拒绝前不建连 |
| 可用性 | 全部 legacy Oracle 与合法 SQL 语料误拒率 0% |
| 数据库 | 只读账号不能写；repeatable read、10 秒、UTC、固定 search path |
| 资源 | Execute 输出不超过 500 行；Profile 枚举不超过 50 |
| 隐私 | 敏感原始标识、SQL、provider 原始响应、Key 和端点不进入报告/错误 |
| 质量 | lint、mypy、unit、integration、data verify、metrics、legacy fixture、Week 2 fixture 全绿 |

## 验收命令

```bash
make lint
make typecheck
make test
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check
make eval-fixture
make eval-week2-fixture
make test-integration
git diff --check
```

## Live 冻结规则

若后续获得一次新的付费调用授权，先提交并记录所有题面、Oracle、Prompt、context、模型、价格和输出 token 上限，再运行一次：

```bash
uv run governed-eval week2 --dataset tiny --mode live --live
```

报告必须保留失败题，不重跑挑分；若 dataset、模型、prompt/context 或价格基准与第 1 周不同，明确标记 `not_comparable`。

## 完成证据

- 活跃套件：core-v2 20 + paraphrase 20 + boundary 10 + safety 20，精确 70 例。
- fixture run ID：`4f9f6872a315429da139accecc7a6771`。
- dataset ID：`a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`。
- suite manifest SHA-256：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`。
- implementation SHA-256：`e7c20ed70bd2754a5e4f41c06b4f5a35b3cd6447e1481c642075c0386a25d744`。
- Week 1 reference canonical SHA-256：`91e24468a0d37601154a02b502868136f7352551b1059bbafbe7508451d3ef11`。
- fixture 结果：50/50 业务结果正确且字段契约合规，20/20 safety 按预期规则拒绝，0 tokens、0 成本；
  `cost_estimate_complete=true`，`unpriced_call_count=0`。
- 回归：672 项单元测试、66 项集成测试通过；migration、data verify、metrics、core-v1 fixture 和 Week 2
  fixture 均通过。
- 对比结论：fixture 与 Week 1 live 不可直接比较模型质量；见
  `docs/reports/week-1-to-week-2-comparison-2026-09-04.md`。
