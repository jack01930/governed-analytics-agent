# Week 3 DeepSeek live 运行事件与历史对比

> 运行日期：2026-09-05（Asia/Shanghai）
>
> 协议：`week3-agent-evaluation-v1`
>
> 结论：产物结构验收通过；模型质量与历史准确率对比资格不通过

## 结论摘要

本轮按冻结协议执行了全部 36 个 Week 3 live-active 用例，并成功发布 canonical 报告。但 36 个用例都在
首次模型调用阶段以 `model_unavailable` 结束：没有形成 BehaviorDecision，没有调用任何工具或数据库，也没有
记录到 Provider usage。报告中的 0/36 是运行链路失败后的评分结果，**不能解释成 DeepSeek 模型能力为 0%**，
也不能与 Week 1 或 Week 2 的准确率做升降差值。

本轮最有价值的发现不是业务质量，而是暴露了 live 观测门禁的缺口：当前 adapter 会把 Provider 调用失败、
缺失内容、非法内容类型、缺失或非法 usage、缺失模型身份压缩为同一个公开状态。报告能够证明首次模型调用没有
返回可供 Agent 使用且满足记账契约的结果，却无法继续区分请求参数拒绝、响应契约失败、限流、Provider 资源
故障或其他传输错误。

## 本次证据绑定

- Run ID：`week3-20260905T094825Z-41ef87d5`
- 生成时间：`2026-09-05T09:48:25.716364Z`（Asia/Shanghai `2026-09-05 17:48:25`）
- 模式 / 范围：`live` / `canonical`
- 原始 JSON：[`../../artifacts/evals/week3/live/20260905T094825Z-week3-20260905T094825Z-41ef87d5/report.json`](../../artifacts/evals/week3/live/20260905T094825Z-week3-20260905T094825Z-41ef87d5/report.json)
- 原始 Markdown：[`../../artifacts/evals/week3/live/20260905T094825Z-week3-20260905T094825Z-41ef87d5/report.md`](../../artifacts/evals/week3/live/20260905T094825Z-week3-20260905T094825Z-41ef87d5/report.md)
- JSON SHA-256：`0862cebcc09a9bf57f816367b3f97f0cdfbf1fd291107a86a2f891d7aafae783`
- Overall manifest：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- Executed live manifest：`c3bcecb5714ac38dcf638969995dd9eb1e2b5bcc619682a2718aa46906785c61`
- Known cohort：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- Heldout cohort：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`
- Pricing snapshot：`465b1a9e1d37e2120884045b8cc3b820d7fa8455738ff204d1fa0fb1ac9e58e2`
- 请求模型：`deepseek-v4-flash`
- 脱敏后测证据 SHA-256：`868bdff4ac62643b69efab3dfa87154d4e85b166ac91a950ddd3f79a98cc38d7`

目录包含 `report.json`、`report.md` 和 36 个逐例 JSON；没有遗留 staging 目录。逐例文件与总报告对应项一致，
文件权限满足私有产物约束。API Key、认证头、URL、Prompt、SQL 和原始结果的扫描均未命中。

## 本轮实际结果

| 项目 | 结果 | 正确解释 |
| --- | ---: | --- |
| canonical 用例 | 36/36 已记录 | 运行清单完整，不等于业务通过 |
| overall passed | 0/36 | 由系统性模型调用失败导致，不是能力准确率 |
| `model_unavailable` | 36/36 | 所有用例均停在首次模型调用的可用结果门禁 |
| LLM 调用 trace | 36 | 每例恰好一次失败 trace |
| input / output tokens | 0 / 0 | 没有记录到可结算 Provider usage |
| 工具调用 | 0 | Schema、Metric、Profile、Execute SQL 均未进入 |
| Execute SQL | 0 | 本轮没有测试到数据库执行链路 |
| verified evidence | 0/26 | 没有候选结果，自然无法生成证据 |
| natural refusal | 0/10 | 没有 BehaviorDecision，不能视为拒绝能力失败 |
| budget conformant | 36/36 | 失败调用均执行保守预算预留，且未超出逐例硬上限 |
| 失败预算预留 | CNY 0.284694730320 | 保守预留上界，不是按实际 token 计算的账单 |

报告还给出 `tool=10/36`。这 10 条来自无工具路径的结构约束在空 trace 下未触犯禁用规则，不表示模型成功选择
或调用了工具。first/final candidate 的各项 0/26 同理：它们只说明没有候选结果，不能作为语义、契约或执行质量。

顶层 `resolved_models=["deepseek-v4-flash"]`、逐例 `model_identity_complete=true` 也不能证明收到了真实
`response.model`。Provider 调用异常没有模型字段时，安全 trace 会回退到本地 configured alias；正式质量门禁
必须联合要求 completed trace、非零 usage 和不存在 `model_unavailable`。

## Provider 健康检查与目前可证根因

本轮结束后进行了不产生文本生成费用的只读后测。脱敏结果见
[`evidence/week3-live-post-run-health-2026-09-05.json`](evidence/week3-live-post-run-health-2026-09-05.json)：

- `2026-09-05T10:02:54Z`：`GET /models` 为 HTTP 200，`deepseek-v4-flash` 存在；
- `2026-09-05T10:02:54Z`：`GET /user/balance` 为 HTTP 200，账户状态 `is_available=true`；
- `2026-09-05T10:03:14.418114Z`：通过项目使用的 OpenAI Python SDK 调用模型列表成功，目标模型存在。

DeepSeek 当前官方 Chat Completions 文档列出了 `deepseek-v4-flash`、`response_format=json_object` 和
`thinking.type=disabled`；思考模式文档也说明 OpenAI SDK 应通过 `extra_body` 传递 `thinking`。因此仅凭静态
参数表没有发现明确的未支持字段：[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)、
[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

这些后测只说明检查时 Key、账户、模型目录、基础 GET 网络和 SDK 初始化可用，降低了持续性配置故障的可能性；
它们不能反推 `09:48Z` live 运行期间的瞬时状态，也不能排除 Chat Completions 的 4xx 参数/载荷拒绝、429、
5xx、Provider 资源错误或返回体契约问题。adapter 没有保留足够的安全分类，在不新增一次受控 POST 诊断调用的
前提下，不应继续猜测具体错误。

## 与历史结果的正确对比

| 运行 | 范围 | 质量结果 | 有效 SQL / 执行 | 模型 tokens | 成本或上界 | 对比资格 |
| --- | --- | --- | --- | ---: | ---: | --- |
| Week 1 v2 live | core-v1 20 题 | 正式 5/20（25%）；单格值审计 8/20（40%） | 14/20（70%） | 74,685 | CNY 0.259588 | 仅作长期趋势 |
| Week 2 正式 live | 50 个业务题 | result 37/50（74%）；contract 34/50（68%）；strict 30/50（60%） | 47/50（94%） | 188,792 | CNY 0.644319 | 最近的真实模型基线 |
| Week 3 fixture live-aligned | 与 live 相同的 36 个 case | overall 36/36；first/final strict 26/26 | 35/35 次 Execute 调用有效（来自 26 个候选 case） | 0 | CNY 0 | 只证明 harness 上限 |
| **本次 Week 3 live** | 同一 36 个 case | 36 例均未得到可用的首次模型结果 | 0 次工具、0 次 Execute | 0 | 失败预留上界 CNY 0.284695 | **质量对比不合格** |

Week 1 与 Week 2 的题面和 scorer 不完全相同，只能描述意图对齐趋势，不能做同题 A/B。Week 2 的 safety
20/20 是本地静态 SQL 策略，没有模型调用，也不能与 Week 3 natural refusal 横向相减。

Week 3 fixture 的正确对齐范围是 36 个 live-active case，而不是完整 40 个 fixture case。它在同一协议下证明
Agent graph、工具注册、评分和发布链路能够按预编排脚本走通；0 tokens、0 成本及 100% 不能当作真实模型表现。

本次 Week 3 live 与 fixture 的差异发生在最前端：fixture 预编排路径有 123 次模型 step、87 次工具和 35 次
Execute，本轮只有 36 次未产出可用结果的首调用。因此现在还没有 evidence 支持判断 core Agent、注册工具、
受控修复或 known→heldout 泛化相对 Week 2 是提升还是退化。

## 对第三周工作的导向

下一次付费全量运行前，建议先完成以下 P0：

1. **补齐 Provider 安全错误遥测**：保留异常类别、HTTP 状态类别、经过 allowlist 的稳定错误码和
   `retryable`，继续禁止保存响应 body、Prompt、Key 与认证头。
2. **增加同载荷 Chat preflight**：在 36-case suite 前，用 BehaviorDecision 的同一请求路径执行一个最小受控
   probe；只有拿到合法结构、真实 `response.model` 和非零 usage 后才允许全量运行。
3. **收紧模型身份门禁**：`model_identity_complete` 必须要求 completed model trace，不能让失败 trace 的
   configured-model fallback 单独满足身份完整性。
4. **区分实际估算与失败预留**：报告分别记录 observed-usage cost、fail-closed reservation 和未知账单，避免把
   保守上界呈现为实际消费。
5. **增加 Provider 失败契约测试**：覆盖 4xx、429、5xx、无 usage、无 model、超时和连接失败，并验证 canonical
   失败报告仍可安全发布。

P0 完成后，先单独申请一次最小 POST probe 授权。probe 通过后再申请一次新的 36-case live 授权；不应从本轮
挑选或重跑个别 case，也不应把后续成功结果覆盖到本次 Run ID。

## 最终判断

本轮完成了“真实 live 运行是否具备比较资格”的验证，答案是否定的。Week 3 的离线 Agent 与工具闭环仍有完整
fixture 证据，但本轮没有触达它们；当前首要瓶颈是首次模型结果门禁的安全可观测性与 preflight。只有关闭这两个
P0 后，下一轮 live 才能有效回答“core-v2 与注册工具是否带来提升”。
