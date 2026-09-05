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

## Fix round 1（2026-09-05）

### 修复结果

- 报告 reservation 改为持有 parent/staging directory FD；所有固定文件均通过
  `dir_fd + O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC` 创建并逐文件 `fsync`，
  cases 目录同样使用 FD。发布前按 FD 校验精确 inventory 和 regular-file 类型，native
  no-replace rename 使用 parent FD，发布后以仍打开的 staging FD 校验最终 inode。
- source swap 或 postcheck mismatch 时，错误 final 会从固定最终名称原子隔离；owned inode
  通过 parent FD 重新定位并清理，foreign inode 不删除。FD close 状态逐项记录，可只重试失败项；
  cleanup/close 失败不遮蔽原异常。
- `Week3CaseResult.passed`、conformance flags、trace/budget/evidence counts 均改为 computed
  fields；behavior/tool/evidence/budget/terminal/suite 使用安全详细字段确定性重算或严格交叉校验。
- 正式报告强制 canonical fixture 40 / live 36 的固定顺序、cohort、suite 和三份冻结 manifest
  hash；executed hash 由 protocol overall hash、mode、ordered case IDs 共同派生，无全零豁免。
  自定义子集只能标记为 `partial_test`，case IDs 明确输出。
- 所有报告字符串字段使用受限 Identifier；model ID 使用
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` 并拒绝 `sk-`、`pk-`、`bearer` 前缀。
  writer 在重新验证 Pydantic model 后递归扫描 key/value，拒绝 SQL、prompt/question、raw rows、
  payload、URL/endpoint 和 credential 形态；覆盖 `model_copy`/`model_construct` 绕过。
- runner 在评分前核对 `AgentRunResult.run_id == case_id`，known→heldout replay 映射为固定
  `case_identity_mismatch` 脱敏失败并继续。
- W3K027/028 固定两个相同 Execute 安全三元组并验证 first 为最早 observation、首次 validation
  invalid 及 final valid/no-valid 分支；W3K029 固定 confirm triple 和完整 verified evidence 链；
  W3K030 固定失败 simple triple、允许的 policy diagnostic、零成功 observation/evidence/repair。
- Execute scorer 对 `(purpose, contract_id, hypothesis_id)` 做精确 multiset；Profile 可插入，
  attribution 的三个维度仍可换序。observation/validation/trace 以完整安全 metadata 做双射，
  validation fingerprint 全局唯一；orphan/duplicate/mismatch/evidence-only 均显式零分。
- 新增 `MetricCount(numerator, denominator, rate)`；JSON/Markdown 分开输出 first/final 的
  result、alias contract、production validation、execution、truncated、strict，以及 repair、
  valid execute、natural refusal、known/heldout、known behavior/simple/attribution、tool/evidence/budget。
  nullable 指标只使用 applicable denominator，Markdown 用 `N/A` 表示零适用分母。

### RED / mutation 回归

- 命令：
  `uv run pytest tests/unit/evals/week3/test_scorers.py::test_tool_trace_rejects_extra_or_duplicate_execute_safe_triples tests/unit/evals/week3/test_runner.py::test_runner_rejects_known_result_replayed_for_heldout_case tests/unit/evals/week3/test_reporting.py::test_report_reservation_rejects_symlink_in_parent_path tests/unit/evals/week3/test_models.py::test_week3_report_aggregates_are_derived_from_cases_only -q`
- 输出：`4 failed`；分别证明 exact Execute triple、run identity、父路径 symlink 和全零
  executed manifest 在修复前均未被拒绝。
- 最终定向命令：
  `uv run pytest tests/unit/evals/week3/test_models.py tests/unit/evals/week3/test_scorers.py tests/unit/evals/week3/test_runner.py tests/unit/evals/week3/test_reporting.py -q`
- 输出：`39 passed in 8.99s`。

### 最终验证

- `uv run pytest tests/unit/evals/week3 -q` → `347 passed in 24.35s`。
- `DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics uv run pytest tests/integration/evals/test_week3_runner.py -q`
  → `1 passed in 15.35s`；覆盖 40/40 fixture、special suite exact triples、W3K029
  verified evidence、W3K030 backend=0，以及逐 case backend 归因。
- `make check` → Ruff `All checks passed!`；mypy
  `Success: no issues found in 156 source files`；unit `1521 passed in 46.80s`。

### 剩余关注

- 按禁令未执行 live/付费/API client/network 调用；live provider 的实际质量和生命周期仍归 Task 14。
