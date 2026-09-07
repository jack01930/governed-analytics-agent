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

## Fix round 2（2026-09-05）

### 修复结果

- 报告目录从文件系统根开始逐级使用 `dir_fd + O_DIRECTORY + O_NOFOLLOW` 打开或创建，
  不再先扫描后按 pathname 创建。`report.json`、`report.md` 与每个 case JSON 的 FD 均保持到
  publish 后校验完成，并绑定创建时的 device/inode/size/SHA-256；cases 目录同样绑定 FD identity，
  发布前后都核对精确 inventory。普通文件替换、同 inode 内容篡改、component/source swap 均
  fail closed，fixed final 不可见，检测到 foreign 内容时将其隔离保留。
- publish 后已完成身份与内容校验即采用成功语义：file/cases/staging/parent FD 全部逐项关闭，失败项
  最多重试四轮，单项错误不阻止其余 FD 的关闭，也不把已经验证的 final 改成失败；publish 前或
  postcheck 失败仍隔离 fixed final，cleanup/close 异常不遮蔽原异常。
- canonical case ID 现在固定绑定 cohort、suite、expected behavior/missing fields、terminal status/
  reason 与 repair override；run budget configuration 与每个 case 的 action/model/tool/execute/profile/
  repair/hard-cost limits（含 W3K029 tool=3）严格交叉验证。
- 新增安全 validation refs、first observation ID 与逐 case model usage/expected resolved identity。
  `SuiteScore`、`model_identity_complete`、`valid_execute_count`、`repair_succeeded`、
  `natural_refusal` 均由这些安全明细唯一派生或严格绑定；writer 重新验证后再发布，可拒绝
  `model_copy`/`model_construct` 绕过。
- recursive guard 删除测试专用裸 `sentinel` 规则，合法 `sentinel-llm` 与普通
  `selected_from_cache` 不再误报；SQL 语句、URL、credential 形态与禁止 key 仍统一拒绝且不回显。
- W3K030 仅接受精确 `read_only_policy` failed Execute diagnostic；`sql_timeout`、
  `database_error` 等不能伪装 policy rejection。runner budget gate 增加 hard cost 上限；超限 case
  保留原始安全 token/cost 明细并形成 nonconformant result，不再降级成全零
  `scoring_contract_failure`。

### RED / mutation 回归

- 显式 mutation：暂时移除 file identity 校验、恢复裸 `sentinel` 拒绝、放宽 W3K030 为任意
  `SafeSqlDiagnostic`、移除 hard-cost gate。
- 命令：
  `uv run pytest tests/unit/evals/week3/test_reporting.py::test_written_regular_file_inode_swap_is_rejected_and_isolated tests/unit/evals/week3/test_reporting.py::test_file_swap_after_publisher_identity_check_is_rejected tests/unit/evals/week3/test_reporting.py::test_recursive_boundary_allows_plain_sentinel_and_sql_keywords_in_prose tests/unit/evals/week3/test_runner.py::test_policy_suite_accepts_only_exact_read_only_rejection tests/unit/evals/week3/test_runner.py::test_hard_cost_exceed_preserves_safe_usage_in_nonconformant_result -q`
- 输出：`8 failed, 1 passed in 4.34s`；三类写后 inode swap、publisher seam swap、合法 model ID、
  两个错误 policy diagnostic 与 hard-cost usage 保留均被 mutation 捕获。
- 恢复实现后同一命令：`9 passed in 3.98s`。

### 最终验证

- `uv run pytest tests/unit/evals/week3 -q` → `361 passed in 26.99s`。
- `DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics uv run pytest tests/integration/evals/test_week3_runner.py -q`
  → `1 passed in 15.22s`；覆盖 canonical fixture 40/40、W3K030 精确
  `read_only_policy` 与 per-case backend=0。
- 定向 Ruff/mypy：`All checks passed!`；`Success: no issues found in 15 source files`。
- `make check` → Ruff `All checks passed!`；mypy
  `Success: no issues found in 156 source files`；unit `1535 passed in 48.40s`。

### 剩余关注

- 按禁令未执行 live/付费/API client/network 调用；live provider 实际质量与 engine/client 生命周期
  仍归 Task 14。

## Fix round 3（2026-09-05）

### 修复结果

- 报告失败清理不再递归删除目录内容，只删除创建时已记录、且当前仍满足
  device/inode/type/size/SHA-256 绑定的 owned leaf/directory。任何 extra、replacement 或同 inode
  内容篡改都视为 foreign，并通过 parent FD 原子隔离；无论发生在 publish 前还是 publish 后，
  固定 final 名称均不可见，foreign 内容保留。
- 新增进程内、`repr=False` 且不进入任何输出的 `Week3PublicationEvidence`。runner 在调用任何 case
  前把 canonical frozen expected 读成内存冻结值，并连同原始公开 `AgentRunResult`、case registry、
  settings 与 pricing/model identity 交给 writer；writer 独立重跑 scorer，与拟发布 report 全字段
  比较，只序列化重建结果。同步伪造 candidate、validation、model identity 或 aggregate 不能发布。
- `SafeValidationRef` 增加 purpose/contract/hypothesis/query/columns/row-count/truncation 与安全 result
  digest；每条 completed Execute trace 必须与 validation ref 精确 multiset 双射，valid ref 必须具有
  严格恢复后的 result digest，evidence 继续核对同一 observation/query/hypothesis。
- recursive guard 使用 sqlglot PostgreSQL statement classification，拒绝 SELECT、VALUES、GRANT、
  COPY、DML、DDL 与 CTE 等完整 SQL 值，同时允许 `please select a cached model`、`drop shipping is
  selected from cache` 与 `sentinel-llm` 等普通文本。
- FD close 按正式 ruling 改为 one-shot detach：调用 `close(2)` 前即从 reservation ownership 状态释放，
  同一整数只尝试一次；异常记录为 `close_unknown` 并继续尝试所有其他 FD，绝不因复用同编号而误关
  unrelated FD。已完成 post-publish identity/digest 校验的 final 仍成功返回，publication validity 与
  resource close state 分离。本节取代 Fix round 2 的“四轮重试”旧描述。
- directory walker 显式分离 old/child ownership；parent close 异常时仍回收 child，外层异常处理不会
  再次 close 状态未知的 old FD。缺失 validation 等 scorer contract 问题转成固定
  `scoring_contract_failure` case 并继续生成报告，不破坏整次 run。

### RED / mutation 回归

- Fix round 2 review baseline 的六类 mutation 分别可绕过 foreign cleanup、candidate/model 自报、自然
  文本 SQL 误判、FD 重试/泄漏和 walker child ownership；本轮新增确定性 probe，直接注入 cases-dir
  replacement、root/cases extra regular/dir/symlink、held-FD same-inode tamper、独立 evidence 同步伪造、
  close-then-dup2 FD reuse、persistent close failure 与 parent-close exception。
- 定向命令：
  `uv run pytest tests/unit/evals/week3/test_reporting.py tests/unit/evals/week3/test_runner.py::test_runner_freezes_expected_results_before_invoking_any_case tests/unit/evals/week3/test_runner.py::test_missing_validation_becomes_explicit_case_failure_not_run_failure -q`
- 输出：reporting 独立回归 `57 passed in 9.60s`；两个 runner publication-evidence/fail-closed 回归
  `2 passed in 6.22s`。所有错误断言使用固定脱敏诊断，不回显注入内容。

### 最终验证

- `uv run pytest tests/unit/evals/week3 -q` → `398 passed in 51.00s`。
- `DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics uv run pytest tests/integration/evals/test_week3_runner.py -q`
  → `1 passed in 17.96s`；覆盖 canonical fixture 40/40、completed Execute ↔ validation ref
  一对一、valid result digest、special suites 与 W3K030 backend=0。
- 定向 Ruff/mypy：`All checks passed!`；`Success: no issues found in 15 source files`。
- `make check` → Ruff `All checks passed!`；mypy
  `Success: no issues found in 156 source files`；unit `1572 passed in 73.60s`。

### 剩余关注

- 按禁令未执行 live/付费/API client/network 调用；`close_unknown` 仅在 OS 无法确认 close 状态的罕见
  分支保留到进程退出，避免重试同一整数误关复用 FD。live provider 与 engine/client 生命周期仍归
  Task 14。

## Fix round 4（2026-09-05）

### 修复结果

- regular-file publication validation 改为从目标 parent `dir_fd` 使用 `O_NOFOLLOW` 重开并持有独立
  verification FD，同时核对 held FD、verification FD 与 directory entry 的 device/inode/type/size；
  两个 FD 分别计算 SHA-256，digest 完成后再次核对 directory entry，发布前和发布后均执行。
  parent `fsync` 后再做最终 postcheck，覆盖 pathname stat 与 held-FD digest 之间及 postcheck 内部的
  inode swap。
- cleanup 不再使用外层 match 结果直接按名删除。owned leaf 在 `_unlink_bound_file` 内重开、核对完整
  identity/size/digest 并在紧邻 `unlink` 前再次 stat；cases/staging directory 在 `_rmdir_bound_directory`
  内以 held FD + nofollow verification FD 核对 identity 与空 inventory，并在紧邻 `rmdir` 前再次 stat。
  replacement、extra 或篡改内容只会被隔离/保留；fixed staging/final 若换入 foreign inode，则按原
  staging identity 寻找并隔离 owned root，绝不移动或删除 foreign fixed name。
- runner 在任何 `run_case` 调用和 report reservation 前建立完整 `_ProtocolSnapshot`：从文件系统根开始
  逐级 `dir_fd + O_DIRECTORY + O_NOFOLLOW` 打开 Week3 树；registry、scripts、candidate SQL、Oracle 与
  expected leaf 均以同一 FD 双读，并做 fstat before/after、第二个 nofollow verification FD、directory
  entry 及 parent-directory metadata 稳定性核对。snapshot 在内存中解析 canonical cases 与 frozen
  expected result，并重新计算、核对固定 overall/known/heldout 三份 manifest hash。
- scorer 和 writer rebuild 只消费该 immutable snapshot 投影；`Week3PublicationEvidence` 绑定完整
  canonical cases、三份 snapshot hash 与内存 frozen expected，不再调用 pathname loader。snapshot
  失败使用固定 `Week 3 protocol snapshot is unavailable`，在 executor 调用数为零时终止，不回显
  registry、SQL、rows 或 parser/OS 原始诊断。
- SQL value guard 先做无日志的完整 statement 前缀/结构分类，仅对 SQL 候选调用 PostgreSQL parser；
  Command/Transaction/Set/Analyze 类中的 `VACUUM`、`CALL`、`BEGIN`、`COMMIT`、`SET`、`ANALYZE` 连同
  SELECT/VALUES/GRANT/COPY/DML/DDL/CTE 全部拒绝。普通 `please select a cached model`、
  `drop shipping is selected from cache` 和 `sentinel-llm` 仍允许，caplog/capsys 不含输入。
- 保持 Fix round 3 的 one-shot close ruling：每个 FD 仅尝试关闭一次，`close_unknown` 与 publication
  validity 分离；本轮未恢复任何同编号 FD 重试。

### RED / mutation 回归

- 命令：
  `uv run pytest tests/unit/evals/week3/test_reporting.py::test_binding_validation_rejects_swap_between_path_check_and_digest tests/unit/evals/week3/test_reporting.py::test_cleanup_rechecks_leaf_binding_immediately_before_unlink tests/unit/evals/week3/test_reporting.py::test_recursive_boundary_rejects_complete_sql_statements_without_parser_leaks tests/unit/evals/week3/test_runner.py::test_runner_rejects_expected_leaf_symlink_before_executor -q`
- 输出：`8 failed, 12 passed in 1.47s`。失败分别证明 cleanup outer-check→unlink 可删除 foreign leaf、
  VACUUM/CALL/BEGIN/COMMIT/SET/ANALYZE 未拒绝且 Command fallback 会记录输入，以及 runner 尚未建立
  handle-bound protocol snapshot。随后补充 postcheck-internal swap、cases/staging bound-rmdir、
  publish-postcheck→quarantine fixed-final swap、parent component symlink、open/identity/read/restore source
  swap 与 same-inode tamper mutations。

### GREEN 与最终验证

- reporting + runner 定向：
  `uv run pytest tests/unit/evals/week3/test_reporting.py tests/unit/evals/week3/test_runner.py -q`
  → `86 passed in 5.56s`。
- Week3 unit：`uv run pytest tests/unit/evals/week3 -q` → `416 passed in 20.67s`。
- 本地 40-case integration：
  `DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics uv run pytest tests/integration/evals/test_week3_runner.py -q`
  → `1 passed in 13.04s`。
- 定向 Ruff → `All checks passed!`；全量 mypy →
  `Success: no issues found in 156 source files`。
- `make check` → Ruff/mypy 通过，unit `1590 passed in 43.73s`。
- `git diff --check` → PASS。

### 自审与关注

- 已逐项复核 four findings：validation 与 delete helper 都在 digest 后重新绑定 directory entry；所有
  已暴露 Python helper seam 的 mutation 均 fail closed；unknown inventory 不递归删除；publisher
  postcheck 与 quarantine 只按 owned root identity 操作；snapshot 在任何 executor 调用前完成且后续
  scorer/writer 不再读取 protocol pathname；SQL parser 不输出原文。
- 按 Task12 staging trust model，拥有相同 UID 的攻击者在最后一次 identity check 与原生
  `unlinkat/rmdirat/renameat` 的纳秒级 syscall 边界仍无法由 Python 消除，本轮未声称解决该已 ruling
  的边界。
- 按禁令未执行 live/付费/provider/API/network 调用；仅运行本地 fixture PostgreSQL integration。

## Fix round 5（2026-09-05）

### 修复结果

- report value SQL guard 改为两段式无回显分类：命令类 statement 使用无日志的 lexical structural
  规则直接拒绝，新增覆盖 `MERGE INTO`、`TRUNCATE TABLE`、`CREATE OR REPLACE VIEW`、
  `CREATE MATERIALIZED VIEW`、`SET LOCAL` 与 `VACUUM(FULL)`；仅 `SELECT` 或具备
  `identifier AS (` CTE 结构的 `WITH` 候选进入 PostgreSQL AST parser。parser failure 被固定为
  非 SQL 候选，不输出或抛出携带原文的诊断。
- 普通 prose 与合法 model ID 不再被粗粒度 SQL 前缀误判：显式验证
  `select a cached model`、`with cached model metadata`、
  `drop shipping is selected from cache`、`grant-model` 与 `sentinel-llm` 可发布；正反矩阵均捕获
  caplog/stdout/stderr，确认无输入泄漏。unsafe boundary 仍统一返回固定
  `Week 3 report contains unsafe metadata`。
- `_validate_file_binding()` 的 verification FD 改为 one-shot close：完成 identity/content/entry
  验证后只调用一次 `close(2)`，close 状态未知时记录到 reservation `close_unknown`，不重试同一整数。
  validation 主异常不会被 close 异常遮蔽；已经完成 post-publish validation 的 final 不会因资源回收
  异常被反向隔离，publication validity 与 resource status 保持分离。
- 新增 pre/post-publish 正常 close、close-before-syscall、close-after-real-close 后 `dup2` 复用、持续
  close error 以及 validation-first failure 的确定性回归；断言每个整数仅尝试一次、其他 FD 继续处理、
  复用 FD 不被误关、成功 final 保留且失败 final 不可见。

### RED / mutation 回归

- 定向命令：
  `uv run pytest tests/unit/evals/week3/test_reporting.py::test_recursive_boundary_rejects_complete_sql_statements_without_parser_leaks tests/unit/evals/week3/test_reporting.py::test_recursive_boundary_allows_prose_and_model_ids_without_parser_leaks tests/unit/evals/week3/test_reporting.py::test_verification_fd_close_error_does_not_reverse_valid_publication tests/unit/evals/week3/test_reporting.py::test_persistent_verification_fd_close_errors_attempt_every_fd_once tests/unit/evals/week3/test_reporting.py::test_verification_fd_close_error_does_not_mask_validation_failure -q`
- 修复前输出：`15 failed, 20 passed in 1.56s`。六个新增 PostgreSQL statement 漏过边界；三个合法
  prose/model-id 被拒绝，其中 Command fallback 记录了输入；四个 pre/post close-error case、持续错误
  case 与 validation-first case 分别证明 publication 被反转或主异常被遮蔽。
- 修复后同一定向集：`35 passed in 1.30s`；另加入 pre/post 正常 close 显式一次性回归。

### 最终验证

- Week3 unit：`uv run pytest tests/unit/evals/week3 -q` → `436 passed in 21.37s`。
- 本地 canonical fixture 40-case integration：
  `DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics uv run pytest tests/integration/evals/test_week3_runner.py -q`
  → `1 passed in 13.15s`。
- `make check` → Ruff `All checks passed!`；mypy
  `Success: no issues found in 156 source files`；unit `1610 passed in 43.40s`。
- `git diff --check` → PASS。

### 剩余关注

- 按禁令未执行 live/付费/provider/API/network 调用；仅使用本地 fixture PostgreSQL。
- `close_unknown` 仍表达 OS close 结果不可知；依 ledger ruling 不对同一整数做第二次 close，允许该罕见
  分支保留到进程退出。未处理 ledger deferred Minor `Week3PublicationEvidence.__all__`。
