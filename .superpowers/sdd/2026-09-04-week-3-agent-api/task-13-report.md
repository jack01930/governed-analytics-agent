# Task 13 实施报告：Week3 scorer、runner 与原子报告

## 交付结果

- 新增独立 Week3 scorer；未导入 Week2 scorer，且仅消费公开 `AgentRunResult`、冻结 expected result 与安全 trace。
- 新增 fixture/live `Week3CaseExecutor` seam 和严格顺序 runner。runner 只传 `case_id`、`question`，单 case 普通异常转固定脱敏失败并继续；取消、`KeyboardInterrupt`、`SystemExit` 传播。
- 新增 fixture executor：每 case 新建 `ScriptedAgentModel`、`BudgetLedger`、`InMemoryTraceRecorder`、`StructuredModelInvoker`、事件 sink/context；heldout 使用当前问题重键已审核 known steps；共享 Registry/backend 不被关闭。
- 新增 live executor：构造期绑定 requested model 与 pricing；逐 case 校验 resolved model 和 usage 汇总，缺失或不一致 fail closed；不创建、不关闭 client/engine。
- 每 case 使用真实 `asyncio.timeout(settings.timeout_seconds)`；仅 `timeout.expired()` 为真时映射 `TASK_TIMEOUT`，同时保留可安全取得的 budget/trace。
- 新增原子 no-replace 报告预留与发布，writer 返回真实 JSON/Markdown/目录路径；失败只按 inode 清理自身 staging，不覆盖历史目录。

## Scorer 契约

- Execute payload 先严格恢复生产 `tools.contracts.QueryResult`，并核对 observation 外层 `query_id`、`columns`、`row_count`、`possibly_truncated`，再投影为 eval `QueryResult`。
- `CandidateScore` 独立保留 result、alias/output contract、生产 validation、execution、truncation；`strict_pass` 是这五项的不可伪造 AND。
- first 必须是 `first_candidate` 且与 observations 中同 ID 的完整对象相等；final 是同 contract 历史中最后一个具有唯一关联 valid validation、严格 payload 和安全 trace 关联的 Execute。W3K028 不回退到无效候选。
- Evidence 按 required purpose 覆盖计分，不按 evidence 行数；验证 evidence→observation→unique valid validation 的 contract/hypothesis/query 关联。
- 工具顺序实现 partial order：`metric_lookup < schema_lookup < execute_sql`；归因的 `confirm_decline` 先于三个维度，维度之间任意顺序，Profile 可安全插入。
- scorer 不读取 LangGraph State、candidate SQL、fixture script 或 Oracle SQL，也不反查数据库。

## 报告 schema 与脱敏边界

- `Week3CaseResult` 包含 first/final candidate score、behavior action/reason/missing、tool/evidence/budget score、安全 ToolCall/Evidence references、终态、per-case resolved model 与 identity/usage completeness。
- `Week3RunReport` 包含协议/known/heldout/executed manifest hashes、requested/resolved model、pricing snapshot hash/effective date、预算与并发摘要。
- case/pass rate、known/heldout、behavior/simple/attribution、first/final、tool/evidence/budget 全部由 cases 计算；非零 executed manifest 必须由 mode + ordered case IDs 派生，拒绝伪造。
- JSON 类型层面不表达 question、free-text answer、result summary、payload/rows、SQL/params、prompt/model request、Oracle query ID、endpoint/key、pricing source、monotonic deadline。
- fixture Markdown 明确标记为 harness validation，不宣称 live model quality；Week2 safety 不进入 Week3 分母或 JSON。

## RED / GREEN 证据

- RED：`uv run pytest tests/unit/evals/week3/test_scorers.py tests/unit/evals/week3/test_runner.py tests/unit/evals/week3/test_reporting.py -q`，三个新模块均以 `ModuleNotFoundError` 失败。
- GREEN：`uv run pytest tests/unit/evals/week3 -q` → `330 passed`。
- 静态检查：定向 Ruff clean；`uv run mypy` → `Success: no issues found in 156 source files`。
- 全库 unit：`uv run pytest tests/unit -q` → `1504 passed in 46.08s`；同轮 `make check` 的 Ruff/mypy 已通过。
- 本地 tiny PostgreSQL：`make db-up && make migrate && make data-tiny` 成功。
- integration：`uv run pytest tests/integration/evals/test_week3_runner.py -q` → `1 passed`。

## 40-case integration 指标

- 总体：40/40 contract pass；一个共享只读 engine/backend，executor 不 dispose。
- known behavior：10/10。
- known simple strict：15/15。
- W3K026：四个 required purpose 全部具有验证证据。
- W3K027：一次 repair，最后 valid；W3K028：一次 repair，无 valid final，受控 `repair_failed`。
- W3K029：3 次工具调用后拒绝下一工具，终态 `partial/evidence_partial`。
- W3K030：`sql_policy_rejected`、repair=0；共享 backend 总计数与其余审核 Execute 数一致，危险 SQL backend 调用为 0。
- heldout：使用当前 heldout question 重键 known script，纳入独立 heldout 汇总。

## 未决与所有权

- 按任务禁令未执行任何 live/付费/API client/network 调用；live 实际质量不在本任务验证范围。
- Task 13 不拥有或关闭 shared engine/client。Task 14 仍负责先结束 runs，再关闭 client，最后 dispose engine。
