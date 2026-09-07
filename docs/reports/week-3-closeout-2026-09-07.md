# 第三周工程交付记录

日期：2026-09-07。范围：第三周有界 Agent、FastAPI/SSE、治理和离线评测。
结论：第三周核心工程与本地验收完成；两项已知路由偏差进入第四周，不作为本次阻塞项。
本次仅做文档与交付收尾，未修改生产代码、题库或评分规则，未新增 live 调用。

## 已交付能力

- 15 个受治理指标的 typed plan、结果契约与数值证据。
- GMV 下滑确认，以及区域、SKU、客户分群三维贡献分析。
- 结构化输出修复与结果契约修复分别记账、共享一次修复预算；首次候选与失败阶段保留。
- 四类受控工具、只读数据库、SQL policy、调用/时间/成本边界。
- 评分失败可发布，保留原安全 trace、usage 与预算，不以空 internal_error 覆盖真实执行事实。
- 异步任务创建、状态查询、受控 Trace、SSE 进度与 Last-Event-ID 重放。

## 本次验收

验证时源码提交为 `c3cec5e`；最新生产代码修复为 `d64d7a8`，之后均为分析与文档提交。
收尾提交没有改变被测源码。命令退出码均为 0；本地结果与 [机器可读验收记录](evidence/week3-closeout-2026-09-07.json) 绑定。

| 验证 | 本次结果 |
| --- | --- |
| `make check` | Ruff 通过；mypy 158 个文件通过；1725 项单元测试通过（59.77 秒） |
| `make data-verify` | tiny 数据验证通过 |
| `make test-integration` | 86 项通过（18.33 秒），包含真实数据库、Agent、API/SSE |
| `make eval-fixture` | baseline 20/20，Run `7a6f2215acdf4fd68f026ee038883aac` |
| `make eval-week2-fixture` | 70/70，其中业务 50/50、安全 20/20，Run `22212c50cdcc45c0ab72f568c55e95ea` |
| `make eval-week3-fixture` | 40/40，Run `week3-20260907T084311Z-b1404e4a` |
| 本机 Uvicorn HTTP/SSE | 两条内建演示完成；健康检查 200、创建 202、状态 completed；分别 11/26 个事件且仅一个终态；Last-Event-ID 重放正确 |
| 报告发布校验 | Week3 类型解析及派生字段回算、确定性 Markdown、40 份逐例 JSON 一致、安全扫描、文件权限 0600 |
| 历史保全 | 116 个先前证据文件及六轮迭代 live 原始 report 哈希不变 |

fixture 只证明固定脚本驱动下的系统行为，不能作为模型质量成绩。HTTP 验证同样使用 fixture 和真实本地数据库，未进行新的 HTTP live 测试。

第三周设计中的十项验收门槛对应如下：

| 原门槛 | 本次证据 |
| --- | --- |
| 1. 单元、集成、lint、类型检查通过 | 上述 `make check`、`make test-integration` |
| 2. Week2 协议与旧基线保全 | frozen protocol 单元测试；原 suite 哈希仍为 `ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`；baseline/Week2 fixture 全通过 |
| 3. behavior 10/10，非 execute 不执行数据库 | Week3 fixture 行为分 10/10；scorer 与 runner 集成测试检查非执行轨迹 |
| 4. 15 项简单指标满足数值、契约、执行 | Week3 fixture simple strict 15/15 |
| 5. 先确认下滑再执行三维归因，绑定证据 | `tests/integration/agent/test_graph.py` 的完整归因测试；本机归因演示 6 次工具调用、8 项数值证据 |
| 6. 最多一次修复、保留首次候选、再次失败终止 | `tests/unit/agent/test_graph.py`、`tests/unit/runtime/test_budgets.py`；fixture W3K027/028 |
| 7. 静态安全 20/20，policy 拦截不触库且不修复 | Week2 safety；Agent repair policy 与数据库权限集成测试 |
| 8. 预算边界阻止超限调用 | `tests/unit/runtime/test_budgets.py`；Week3 budget conformance 40/40 |
| 9. SSE 递增、重放、断线独立、单终态 | `tests/integration/api/test_api_sse.py`；额外本机真实 HTTP 演示 |
| 10. Trace/SSE/日志脱敏 | API/SSE 与 tracing 的泄漏测试；报告 `_scan_safe` 校验 |

## 最新 live 证据与已知问题

保留 Run `week3-20260905T180413Z-9122c0d4`：开发回归 **34/36**，三条归因均通过，
34/34 次 Execute 通过生产验证，模型/工具/评分异常为零。120 次模型调用、84 次工具调用，
估算模型费用 CNY 1.386811223952。费用为固定价格快照估算，不是供应商账单。
完整分析见 [六轮迭代报告](week-3-live-iterative-repair-analysis-2026-09-06.md)。

| 问题 | 本次处置 | 第四周验证方向 |
| --- | --- | --- |
| W3K001：模糊的“最近表现”漏问时间窗口 | 已知路由偏差，登记延期 | 通用缺参识别同时检查指标和时间，不硬编码 case ID |
| W3H007：已给六月 UTC 窗口却误判缺少时间 | 已知路由偏差，登记延期 | 验证明示时间窗口及等价表达，避免无谓澄清 |

二者在前一轮曾通过，当前缺少重复运行证据；不得把单轮结果描述成稳定性保证。
不为得到 36/36 修改历史题面、答案或评分，也不继续无界 live 调优。

## 合并检查与交付边界

收尾检查覆盖共享 SQL 后端仍经 policy 和只读事务、评分异常回退保留原始 outcome、修复共享预算、
API/事件生命周期及历史协议保全，并结合上述完整离线门禁。它是本地合并检查，不是外部独立审计。
本次收尾文件包括 README、总计划、启动就绪报告、API 使用指南、评测延期安排和本记录。

第三周分支 `codex/week-3-agent-api` 在本地合并到 `main`，合并消息为“合并第三周 Agent 与 API 工程交付”。
合并提交可用 `git log -1 --merges --oneline main` 定位；本次不推送远程，不声称 GitHub Actions 已重新运行。
保留工作分支与原 worktree，以保全未纳入 Git 的历史原始报告：
`/Users/a0000/Projects/Agent-soft/.worktrees/week3-agent-api/artifacts/evals/`。
各报告相对路径与哈希在机器可读记录中；`/tmp` 日志为本机辅助记录，正式汇总保存在 Git 中。

当前运行状态为内存存储，重启恢复和审批按第五周计划；API 用于本地单进程，尚无身份认证和公网部署验收。
Compose 仅启动数据库，Streamlit、完整启动流程和演示仍属于第四周交付。

## 第四周入口

1. 实现 Streamlit 薄客户端，接入创建任务、SSE、状态与证据展示。
2. 按总计划扩展指标与结果展示，改善上述两类澄清路由。
3. 完成应用与数据库启动流程、可复现演示与 README。
4. 继续用现有题库做开发回归；最终共同测试集延期至所有待比较候选完成后或求职前。

跨周功能扩展有实现证据，但目前无同题同规则的跨周准确率增量结论；已见 heldout 只是历史分组名。
最终统一测评按 [固定测试集规则](../static-benchmark-policy.md) 执行，本次不建立最终题库。
