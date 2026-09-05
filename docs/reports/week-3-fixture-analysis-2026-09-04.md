# Week 3 Agent fixture 离线评测分析

> 运行日期：2026-09-05（Asia/Shanghai）
>
> 协议：`week3-agent-evaluation-v1`
>
> 性质：harness validation，不是 live 模型质量或语言泛化结论

## 本次证据绑定

本报告只读取本次命令写入的两个精确指针/摘要，没有按时间或目录搜索历史报告：

- Week 3 run ID：`week3-20260905T084029Z-2e326b40`
- Week 3 生成时间：`2026-09-05T08:40:29.373421Z`（Asia/Shanghai `2026-09-05 16:40:29`）
- Week 3 report：`/Users/a0000/Projects/Agent-soft/.worktrees/week3-agent-api/artifacts/evals/week3/fixture/20260905T084029Z-week3-20260905T084029Z-2e326b40/report.json`
- overall manifest：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- known cohort：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- heldout cohort：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`
- executed fixture manifest：`44291f95af7daab1dd0b96cbe6fb1377ed0efbf44cc228a7330bb46779587f53`
- baseline fixture run ID：`ad906828dace4ffda760a6cd7be148ec`
- Week 2 fixture run ID：`5cb95110dcb4477598285928b3773782`
- Week 2 suite manifest：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`

## 总体与 cohort

Week 3 fixture 为 canonical 40 例，40/40 通过。known 为 30/30，heldout 为 10/10；后者复用冻结的已知
script steps，但绑定 heldout 自身 case ID 与当前问题，因此只能验证 fixture replay 与协议隔离，不能推断真实模型的
paraphrase 泛化。

| 指标 | numerator | denominator | rate |
| --- | ---: | ---: | ---: |
| overall passed | 40 | 40 | 100% |
| known passed | 30 | 30 | 100% |
| heldout passed | 10 | 10 | 100% |
| known behavior conformance | 10 | 10 | 100% |
| known simple strict | 15 | 15 | 100% |
| known flagship attribution | 1 | 1 | 100% |
| tool conformance | 40 | 40 | 100% |
| verified evidence sufficiency | 27 | 27 | 100% |
| budget conformance | 40 | 40 | 100% |
| natural refusal/clarification | 10 | 10 | 100% |
| repair success | 1 | 2 | 50% |
| valid completed Execute | 37 | 41 | 90.24% |

`repair success` 的 denominator 仅为两个 repair 用例：W3K027 按预期修复成功，W3K028 按协议在一次修复后仍无
valid final 并安全失败。因此 1/2 是两个不同预期终态的独立指标，不代表 overall 失败。`valid completed Execute`
同样按 41 次 Execute 尝试计数，其中包含预期 invalid repair candidate 与 policy rejection，不使用 40 个 case 作为
错误分母。

## First 与 final component

有候选结果的适用集为 26 例。first 与 final 的结果、alias/output contract、production validation、execution、
strict 均为 26/26；truncated occurrence 均为 0/26。truncation 是发生次数，不应反向表述为 0% 通过率。

| component | first | final |
| --- | ---: | ---: |
| result exact | 26/26 | 26/26 |
| alias/output contract | 26/26 | 26/26 |
| production validation | 26/26 | 26/26 |
| execution succeeded | 26/26 | 26/26 |
| strict five-part pass | 26/26 | 26/26 |
| possibly truncated | 0/26 | 0/26 |

repair 套件的 first/final 适用性由独立 suite invariant 表达，不被塞入上述 26 例候选分母：W3K027/028 均证明
first 是最早 matching Execute 且首次 validation invalid；W3K027 有 valid final，W3K028 明确不得 fallback。

## 行为、简单指标与旗舰归因

known behavior 固定口径为 10/10。连同两个 heldout behavior，用例实际路由为 clarify 5、execute 2、refuse 3、
unsupported 2，全部与各自冻结预期一致；非 execute 用例没有数据库 Execute。

known simple 的 15/15 对应固定治理指标顺序：GMV、paid GMV、net revenue、valid order count、average order
value、payment success rate、refund amount、refund rate、active customers、new customers、repeat purchase rate、
customer acquisition cost、conversion rate、stockout rate、campaign ROI。该结果同时满足结果值、列/alias、生产
Answer Contract、执行成功、无截断与 strict conjunction。

旗舰 known W3K026 为 1/1；confirm/decline、region、SKU、segment 四个 required purpose 均有 verified Evidence
与 query ID 引用，工具预算为 6 次 tool / 4 次 Execute。heldout attribution 另列为 1/1，不混入 known 旗舰分母。

## Repair、预算、policy 与资源

- W3K027：一次 repair 后完成；2 次 Execute，suite invariant 通过。
- W3K028：一次 repair 后受控 `repair_failed`；无 valid final，suite invariant 通过。
- W3K029：hard tool budget 在下一工具前拒绝；保留 confirm/decline 的 1/1 verified partial evidence，终态为 partial。
- W3K030：只接受精确 read-only policy rejection；1 次失败 Execute、0 valid Execute、0 repair，终态 policy blocked。
- 全 run 共 139 次 fixture model 调用、101 次 tool、41 次 Execute、0 次 Profile、2 次 repair；input/output token
  与成本均为 0。配置上限为 4 action loops、8 LLM、12 tool、5 Execute、2 Profile、1 repair、60 秒，soft/hard
  cost 分别为 CNY 0.20/0.30。

CLI 为 40 例共享同一个调用方拥有的 engine/tool registry；runner 完成后由 CLI dispose engine。fixture 分支没有
读取 ModelSettings/key/pricing，也没有构造 AsyncOpenAI。live 分支仅实现，未执行。

## 安全、API 与 SSE 回归

`make test-integration` 为 86/86：包括只读角色、事务约束、SQL policy、四类工具、40-case Week 3 runner、FastAPI
status/trace 与 SSE。SSE oracle 扫描 raw frame/comment/field 以及 parsed tree/pairs/safe arguments，并覆盖 credential
label/assignment、SQL statement、caller cancellation、owned/borrowed task 与 same-tick exception；没有未回收 lease、
task、thread 或 engine residue。报告与本分析不含 SQL、raw rows、prompt、endpoint、key 或 Provider 原始响应。

Week 2 本次 fixture 独立保持 static safety 20/20（summary rate 1），不进入任何 Week 3 JSON 或分母。

## 最终 Important 修复

最终证据运行在 HEAD `5eee13f`，并包含四项已关闭的 Important 修复：

- `225e477`：修复终态预算覆盖并收紧内部导出。
- `5b61c9d`：收紧 run task timeout 的起点。
- `5da2206`：将 context 构造纳入 task timeout。
- `5eee13f`：修复 Week 3 publication evidence 验真并绑定冻结 script snapshot。

## 运行门禁与环境说明

- `make check`：Ruff 通过，mypy 覆盖 158 个 source files，1683/1683 unit tests 通过。
- `make migrate`、`make data-tiny`、`make data-verify`：通过；tiny dataset ID 为
  `a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`。
- `make test-integration`：86/86 通过。
- baseline fixture、Week 2 fixture summary、Week 3 fixture pointer：最终均在本地只读数据库环境下通过；run ID
  分别为 `ad906828dace4ffda760a6cd7be148ec`、`5cb95110dcb4477598285928b3773782`、
  `week3-20260905T084029Z-2e326b40`。
- `make db-up` 返回 2，因为健康的 `feat-week-1-data-baseline-db-1` 已占用 127.0.0.1:5432。本 worktree
  的容器当前为 Created 且未发布 host port；没有停止、重启或清理任一容器。后续 host URL 门禁实际连接
  `feat-week-1-data-baseline-db-1`。
- Week 3 direct exact 命令首次因 isolated worktree 没有 `DATABASE_URL` 而失败；随后创建 ignored `.env`，其中
  仅包含 local DB 与 fixture gate 配置、不含任何模型 key，并以完全相同的原命令重跑成功。

## 结论与限制

除 `make db-up` 的既有健康容器端口碰撞外，后续 exact 离线门禁均成功；上述容器拓扑和 isolated worktree
`.env` 是环境事实，不改变 Agent graph、工具、只读数据库治理、评分、预算、报告、API/SSE 与 Week 1/2 回归
已满足离线技术条件。因此可以向用户申请一次、固定 case/预算的 Week 3 live 授权。这里的“可以申请”不是授权
本身；本次没有运行 DeepSeek/live/API/network，也没有产生模型费用。

首次 live 仍可能暴露真实语言理解、规划、工具选择、structured output、repair 和 resolved-model/usage 完整性问题。
fixture 40/40 不应与 Week 2 live 或 Week 1 裸基线做提升差值，也不能作为生产质量结论。
