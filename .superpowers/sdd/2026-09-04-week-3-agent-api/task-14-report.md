# Task 14 实施报告

## Phase A — Task11 SSE carry-over

### 范围与边界

- 本阶段只修改 `tests/integration/api/test_api_sse.py` 的验收 helper 与回归测试；未修改生产 SSE/API、CLI、Makefile、CI 或文档。
- 复用本机已运行的 tiny PostgreSQL；未启动/拉取容器，未访问网络、live 模型或 API client。
- coroutine 由 deadline helper 创建并命名为 owned Task；调用方传入的 Task/Future 明确作为 borrowed，helper 不改名、不取消、不接管回收。

### RED / mutation

先加入 carry-over 回归，未修改 helper 时运行：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle'
.F
1 failed, 1 passed, 5 deselected
```

失败为复合 credential label 的结构化位置未触发拒绝；同一 mutation 矩阵也包含原 helper 未覆盖的 `SELECT 1`、`VALUES`、`GRANT`、`COPY` 与 raw SSE surface。

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'deadline or borrowed or caller_cancellation'
...F.
1 failed, 4 passed, 5 deselected
Future exception was never retrieved
```

失败证明 borrowed Task 在 deadline 后被 helper 取消；同轮的确定性 `call_soon` race 还产生未观察的 same-tick child exception。

### GREEN

- credential label 先 NFKC，再拆 camelCase、casefold，并统一空格、点、斜杠、连字符、下划线；递归检查 dict key/value、通用 pair、`safe_arguments` name/value。
- raw SSE 在 JSON/协议解释前检查整段、每行、comment、field name/value 与 frame pair；失败均为固定 `_SAFE_OUTPUT_ERROR`，动态敏感值不进入 pytest 参数 ID 或断言表达式。
- SQL statement classifier 对 `SELECT`/`WITH`/`VALUES` 复用生产 `validate_sql()` 的解析/拒绝码，对 DML/DDL/GRANT/COPY 等使用保守的已列举命令类别与结构；普通说明文本（包括自然语言中的 `select ... from ...`）保持可发布。
- caller cancellation 时先观察已完成 child；owned pending task 才取消并登记 observation callback。borrowed pending Task/Future 保持原 name/cancel 状态，由创建者 release 并 await。
- same-tick 回归安装并在 `finally` 恢复 loop exception handler，断言 handler 未收到任何未观察异常。

定向：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle or deadline or borrowed or caller_cancellation'
.......
7 passed, 3 deselected in 1.20s
```

真实 tiny DB，Agent→API 正序：

```text
DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.............
13 passed in 2.05s
```

反序：

```text
DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/api/test_api_sse.py tests/integration/agent/test_graph.py -q
.............
13 passed in 2.17s
```

asyncio debug / warnings-as-errors：

```text
PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest --asyncio-debug -o asyncio_default_fixture_loop_scope=function tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.............
13 passed in 2.22s
```

全仓门禁：

```text
make check
All checks passed!
Success: no issues found in 156 source files
1610 passed in 46.08s
```

首次 `make check` 仅发现新增 NFKC 回归中的 fullwidth literal 触发三项 `RUF001`；改为等价 Unicode escape 后重跑通过。

### 残留检查

- owned deadline/caller-cancel 路径等待 cancellation `finally` 完成，并与进入测试前的 pending-task snapshot 精确比较。
- borrowed deadline/caller-cancel 路径由测试创建者设置 release gate 并 await 回收；Task name 与 cancellation state 均保持不变。
- same-tick child exception 的 loop handler 上下文精确为空并恢复原 handler；严格 asyncio debug 轮次无 warning。
- 真实 SSE 路径继续断言 lease 归零、worker thread 集合不变、共享 engine 在同一 lifespan 释放；正序与反序均通过。

Phase A 到此停止，等待 scoped review；尚未进入 Task 14 Phase B。

## Phase A Fix round 1

### RED / mutation

先加入 review 指定的 Unicode assignment、SQL 类别变体、hostile owned coroutine 显式回收与 borrowed Future 回归。原实现的定向结果：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle'
.F
1 failed, 1 passed, 10 deselected
```

首个失败为 `NFKC` 前只识别 ASCII assignment punctuation，导致全角等号值未被拒绝；同一先写入的类别矩阵覆盖 query、CTE、DML、DDL、GRANT 与 COPY 变体。

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'exposes_and_reclaims_owned_hostile or borrowed_future'
F.
1 failed, 1 passed, 10 deselected
```

hostile owned coroutine 回归因缺少 `_OwnedTaskHandle` 显式 seam 而失败；borrowed Future 的 deadline/caller-cancel mutation 已证明原 borrowed 分支未取消 Future。

### GREEN

- credential assignment 对整个候选字符串先做 NFKC，再识别规范化后的 `:`/`=`；parsed value 与 raw SSE comment 都覆盖全角等号、全角冒号及 camelCase label，错误仍固定且不含 operand。
- query/CTE 继续使用生产 `validate_sql()`；命令 classifier 扩充 GRANT 多词 privilege/可选 TABLE、括号子查询 COPY，以及 TEMP/TEMPORARY/UNLOGGED CREATE，测试按六个 SQL 类别组织。既有自然语言正例保持通过。
- `_OwnedTaskHandle` 在 helper 创建 coroutine Task 时直接绑定，不靠全局 task 扫描、task name 或 coroutine 私有元数据识别。取消后给 cooperative task 一个 loop checkpoint 并观察完成；hostile task 被确定性暴露，fault probe 释放私有 gate 后通过 handle await 回收。
- borrowed Task/Future 分支拒绝接收 owned handle，既不取消也不命名。新增原生 Future 的 real-deadline 与 caller-cancel 回归，由创建者 `set_result` 并 await 回收。

### 验证

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle or deadline or borrowed or caller_cancellation or hostile'
.........
9 passed, 3 deselected in 1.27s

uv run ruff check tests/integration/api/test_api_sse.py
All checks passed!

uv run mypy tests/integration/api/test_api_sse.py
Success: no issues found in 1 source file

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
...............
15 passed in 1.88s

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/api/test_api_sse.py tests/integration/agent/test_graph.py -q
...............
15 passed in 1.90s

PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest --asyncio-debug -o asyncio_default_fixture_loop_scope=function tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
...............
15 passed in 2.14s

make check
All checks passed!
Success: no issues found in 156 source files
1610 passed in 44.19s
```

### 残留检查

- hostile owned probe 在 deadline 后明确断言 handle 指向的 named task 仍 pending；只有测试释放 gate 并 `reclaim()` 后才断言 pending-task snapshot 恢复，未声称可强制终止拒绝 cancellation 的依赖。
- cooperative owned deadline/caller-cancel、borrowed Task/Future、same-tick child exception、真实 SSE lease 与 lifespan thread/engine 检查均保持通过。
- 未修改生产 SSE/API、CLI/Make/CI/docs 或 controller ledger；Phase A Fix1 后继续停止等待 review。

## Phase A Fix round 2

### RED / mutation

先扩展 acronym/all-caps credential 的全部结构化位置、column-list COPY、GRANT 自然语言对照，以及 handle 复用拒绝路径：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle or owned_handle_reuse or borrowed_future_rejects'
.FF.
2 failed, 2 passed, 10 deselected in 1.21s
```

- `APIKey`/`APIKEY` 等无分隔 acronym label 未触发拒绝。
- 复用 handle 时 helper 先创建了 Task，留下由测试回收的 introduced pending task；这证明校验顺序不满足 fail-before-create。

只加入 compact credential 精确匹配后再运行 SQL mutation：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle'
.F
1 failed, 1 passed, 12 deselected in 1.20s
```

此时 credential 矩阵已通过，失败推进到 `COPY orders (order_id) TO STDOUT` 未被拒绝，证明 table column-list 变体的独立缺口。

### GREEN

- credential label 在 NFKC/camel/casefold/separator 规范化后增加 compact token 精确 allowlist 的反向安全判断，覆盖 `APIKey`、`APIKEY`、fullwidth all-caps，以及 `CLIENTSECRET`、`DATABASEPASSWORD`、`ACCESSTOKEN`；每个 label 都进入 parsed key/value、generic pair name/value、`safe_arguments` name/value 和 raw SSE comment。
- COPY 使用整串 anchored 结构，分别表达 table、table + optional column-list、parenthesized query，以及 `TO/FROM` target/option；不再依赖只接受单 token table 的窄前缀。
- GRANT 从通用前缀正则拆出整串 anchored classifier，覆盖 privilege-on-object、多 privilege、role membership 与合法 option；`grant access to the cached model` 和既有普通说明保持可发布。GRANT/COPY 不调用可能输出 parser warning 的 command fallback。
- `_await_with_deadline()` 在创建 owned Task 前检查 handle 可用性。复用/预绑定 handle 时关闭尚未启动的原始 coroutine，再返回固定错误；回归断言 coroutine 为 `CORO_CLOSED`、未运行且未创建 Task。borrowed Future 携带 handle 时同样在接管前拒绝，Future 保持 pending/uncancelled 并由创建者完成。

### 验证

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle or deadline or borrowed or caller_cancellation or hostile or owned_handle'
...........
11 passed, 3 deselected in 1.17s

uv run ruff check tests/integration/api/test_api_sse.py
All checks passed!

uv run mypy tests/integration/api/test_api_sse.py
Success: no issues found in 1 source file

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.................
17 passed in 1.93s

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/api/test_api_sse.py tests/integration/agent/test_graph.py -q
.................
17 passed in 1.93s

PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest --asyncio-debug -o asyncio_default_fixture_loop_scope=function tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.................
17 passed in 2.12s

make check
All checks passed!
Success: no issues found in 156 source files
1610 passed in 44.28s
```

### 残留检查

- same-tick child exception、hostile owned 显式回收、cooperative owned、borrowed Task/Future 及真实 SSE lease/thread/engine 检查全部保持通过。
- fail-before-create 回归不依赖 task name 或 coroutine 私有 metadata；异常路径无 pending Task、无未 await coroutine warning。
- 未修改/提交 production API/SSE、Phase B 文件或 controller `progress.md`；Fix2 后停止等待 review。

## Phase A Fix round 3

### RED / mutation

先加入带 prose/provider prefix 的 credential assignment、GRANT/COPY 多语句、`ON ALL TABLES IN SCHEMA`、quoted-semicolon 与 trailing-comment 矩阵：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle'
.F
1 failed, 1 passed, 12 deselected in 1.20s
```

首个失败为 `OPENAIAPIKEY` label/assignment 未拒绝；同一 mutation 已预先包含 parsed key/value、generic pair value、`safe_arguments` value 与 raw comment。

加入 credential suffix 机制后再次运行，失败推进到 SQL 矩阵：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle'
.F
1 failed, 1 passed, 12 deselected in 1.17s
```

此时 credential 全矩阵已通过，SQL 类别中的新增 statement-boundary 变体仍被放行，证明整串 GRANT/COPY fullmatch 不足。

### GREEN

- assignment scanner 对 NFKC 后的每个 `:`/`=` delimiter 检查其前缀是否以 credential lexeme 结尾；compound lexeme 使用边界化 compact suffix，单词 label 使用末 token，覆盖 `Use APIKey`、`please use token`、`OPENAIAPIKEY`、provider prefix、mixed/acronym/fullwidth/casefold。没有 assignment 的正常 prose 保持可接受。
- SQL scanner 使用 sqlglot PostgreSQL `Tokenizer` 获得真实 semicolon token，再按原字符串 offset 分句；quoted semicolon 不分句，line/block comment 中的 semicolon 不分句，trailing comment 保持在下一片段并由安全 normalize 去除。
- 每个 statement 独立分类：query/CTE 继续进入生产 `validate_sql()`；GRANT/COPY 先走 anchored common structure，失败后只有出现 SQL 特征关键词结构才 fail closed，故 `ON ALL TABLES IN SCHEMA`、GRANT+DROP 与双 COPY 均拒绝，而 `grant access to the cached model` 仍允许。
- command 分句只使用 tokenizer，不调用 sqlglot parser 的 fallback `Command`，测试同时断言 sqlglot logger、stdout、stderr 均为空；所有拒绝仍为固定 `_SAFE_OUTPUT_ERROR`。
- Fix1/Fix2 的 owned/borrowed handle、same-tick exception、hostile release/reclaim 全部保留并纳入聚焦复验。

### 验证

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle or deadline or borrowed or caller_cancellation or hostile or owned_handle'
...........
11 passed, 3 deselected in 1.30s

uv run ruff check tests/integration/api/test_api_sse.py
All checks passed!

uv run mypy tests/integration/api/test_api_sse.py
Success: no issues found in 1 source file

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.................
17 passed in 2.19s

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/api/test_api_sse.py tests/integration/agent/test_graph.py -q
.................
17 passed in 2.16s

PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest --asyncio-debug -o asyncio_default_fixture_loop_scope=function tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.................
17 passed in 2.39s

make check
All checks passed!
Success: no issues found in 156 source files
1610 passed in 43.97s
```

### 残留检查

- credential 与 SQL 新矩阵覆盖 parsed/raw/pair 边界，failure traceback 继续由 redaction helper 验证不含 operand。
- asyncio debug + warnings-as-errors 无未观察 exception、未 await coroutine 或 pending-task warning；hostile dependency 仅在测试显式 release 后 reclaim。
- 未修改/提交 production API/SSE、Phase B 文件或 controller ledger；Fix3 后停止等待 scoped review。

## Phase A Fix round 4

### RED / mutation

先把 version/environment qualifier label、quoted role、role option、真实 COPY options 与自然语言对照加入同一 safe-output 边界矩阵：

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle'
.F
1 failed, 1 passed, 12 deselected in 1.14s
```

首个失败是 `api-key-v2` 未拒绝；同一预置 mutation 已将 qualifier label 放入 parsed key/value、generic pair name/value、`safe_arguments` name/value 与 raw SSE comment，证明旧实现只识别精确 base label。

### GREEN

- credential label 现在按 `provider* + credential-base + qualifier*` 的受限语法分类；separator/camel/acronym/compact/fullwidth 均先 NFKC 后统一处理，覆盖 version number、legacy、production 及 `provider/openAIAPIKey/version-02`。赋值扫描仅检查 delimiter 紧邻的 256 字符标签窗口，避免长 SSE/SQL 字符串上的无界后缀枚举。
- label 识别保持 token boundary：`token_count`、`api_key_usage_count`、`secret_sauce` 与无赋值的 `discuss token-v2 usage` 可发布，任意 prose 中间出现 `token` 不被当作凭据字段。
- GRANT 使用 PostgreSQL unquoted/double-quoted identifier（含 doubled-quote escape）、qualified/comma list 和整串 role/object grant 结构；覆盖 ADMIN/INHERIT/SET 的 OPTION/TRUE/FALSE、`GRANTED BY CURRENT_USER`，不再以宽泛 `GRANT ... ON ... TO ...` 前缀拒绝正常说明。
- COPY 使用整串 table/column-list/query source、STDIN/STDOUT/file/PROGRAM target、WITH/WHERE 结构；仅对高置信 target 保留 fail-closed hint。含撇号的 COPY prose及 `copy ... from cache ...` 对照保持可发布，多语句仍由 PostgreSQL tokenizer 分句后逐句拒绝。
- Fix1–Fix3 的 owned/borrowed handle、caller cancellation、same-tick child exception 与 hostile explicit reclaim 回归保持不变。

### 验证

```text
uv run pytest tests/integration/api/test_api_sse.py -q -k 'safe_output_oracle or deadline or borrowed or caller_cancellation or hostile or owned_handle'
...........
11 passed, 3 deselected in 1.36s

uv run ruff check tests/integration/api/test_api_sse.py
All checks passed!

uv run mypy tests/integration/api/test_api_sse.py
Success: no issues found in 1 source file

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.................
17 passed in 2.79s

DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest tests/integration/api/test_api_sse.py tests/integration/agent/test_graph.py -q
.................
17 passed in 2.78s

PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error DATABASE_URL='postgresql+asyncpg://analytics_readonly:***@127.0.0.1:5432/governed_analytics' uv run pytest --asyncio-debug -o asyncio_default_fixture_loop_scope=function tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -q
.................
17 passed in 2.98s

make check
All checks passed!
Success: no issues found in 156 source files
1610 passed in 43.53s
```

### 残留检查

- safe-output oracle 的 unsafe error 仍固定且不回显输入；`caplog`、stdout、stderr 继续断言无 sqlglot 原文或 warning。
- asyncio debug + warnings-as-errors 未发现未观察 exception、未 await coroutine 或 pending-task residue；hostile probe 仍由创建者 release/reclaim。
- 仅修改 Phase A harness 与本报告；未修改 production API/SSE、Phase B、ledger 或 `progress.md`。

## Phase B — CLI / Make / CI 与离线门禁

### RED / mutation

先新增 Week3 CLI、live 双授权/初始化顺序、资源清理、Week2 summary、原子 pointer、Make 与 CI 七条尾部测试，未创建实现文件时运行 brief 的定向命令：

```text
uv run pytest tests/unit/evals/week3/test_cli.py tests/unit/evals/test_week2_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py -v
collected 20 items / 1 error
ERROR tests/unit/evals/week3/test_cli.py
ImportError: cannot import name 'cli' from 'governed_analytics.evals.week3'
```

补入第一版实现后，mutation 推进到真实所有权/并发缺口：

```text
uv run pytest tests/unit/evals/week3/test_cli.py tests/unit/evals/test_week2_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py -q
5 failed, 28 passed in 1.55s
```

失败分别证明 partial-init 测试必须显式提供 database seam，以及仅依赖进程级 file lock 不能为同进程双写提供稳定串行语义。随后加入 per-path thread lock、descriptor/path identity 检查和 partial-init cleanup 回归。

### 实现

- 顶层 `governed-eval` 新增 `week3`，fixture 分支在任何 ModelSettings/key/pricing/client 之前执行。畸形 MODEL 环境与所有 live-only seams 均不影响 fixture，且永不构造 AsyncOpenAI。
- live gate 固定为 tiny dataset → `--live` → AgentRuntimeSettings 双开关 → ModelSettings/key → public pricing requested model → shared engine/tools → `AsyncOpenAI(max_retries=0)`。本阶段只实现并测试，未执行 live。
- Week3 在单一 `asyncio.run()` 内创建一次 caller-owned engine/tool registry；runner 完成后 fixture 只 dispose engine，live 严格 client.close → engine.dispose。partial init、runner 与 cleanup 组合失败均处理所有已拥有资源，primary runner exception 不被 cleanup 覆盖。
- Week2 `_run_week2()` 兼容返回本次 `Week2RunReport`；`--summary-file` 仅投影该对象的 run ID、suite manifest 与 safety rate，不扫描报告目录。
- 两类 pointer 使用 nofollow descriptor walk、受限 temp、flush/fsync、原子 replace、parent fsync、target/temp symlink 拒绝、pre/post foreign inode/content 复核与双写串行。native replace 已完成但 wrapper 抛错、或 exact target 已发布后 parent fsync 失败，按 published/耐久性不确定语义成功返回；foreign target 则固定失败并保留。
- Week3 pointer 只接受 runner 返回 artifact；重新验证 canonical scope、完整 protocol/cohort/executed hash、report model、report dir/file 关系与磁盘 JSON 精确相等后，写入 `report_json.resolve()`。decoy 历史报告不参与。
- Makefile 只增加 `eval-week3-fixture`；CI database job 的 offline tail 精确为 7 条并拒绝 full、network client、URL、MODEL_API_KEY 与 live token。
- README、评测指南与项目计划明确 fixture 只验证 harness/tools/DB/governance/scoring；live 尚未运行或授权，并补充本地 API 启动命令。

### 定向 GREEN

```text
uv run pytest tests/unit/evals/week3/test_cli.py tests/unit/evals/test_week2_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py -q
42 passed in 0.94s

uv run ruff check src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py
All checks passed!

uv run mypy src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
Success: no issues found in 4 source files
```

### Step 6 精确门禁

按 brief 顺序执行，结果如下：

```text
make check
All checks passed!
Success: no issues found in 158 source files
1641 passed in 43.39s

make db-up
Error response from daemon: Bind for 127.0.0.1:5432 failed: port is already allocated
make: *** [db-up] Error 1
```

端口占用者是同仓库另一 worktree 已运行 38 小时的健康本地 PostgreSQL。controller 裁决到达前曾短暂尝试切换
本 worktree 容器；收到“不操作既有容器”的裁决后立即停止新容器并把原容器补偿恢复为 running/healthy，之后
不再操作容器。保留上述首次环境碰撞失败证据，并复用恢复后的本地 PostgreSQL 完成余下门禁；没有修改
Makefile 绕过，也不把短暂重试记作 `db-up` 通过。

```text
make migrate
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.

make data-tiny
scale=tiny dataset=a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2
manifest=artifacts/datasets/tiny/dataset_manifest.json

make data-verify
verification succeeded: scale=tiny manifest=artifacts/datasets/tiny/dataset_manifest.json

make test-integration
86 passed in 25.34s

uv run governed-eval baseline --dataset tiny --mode fixture
exit 0

uv run governed-eval week2 --dataset tiny --mode fixture --summary-file artifacts/evals/week2/week2-fixture-summary.json
exit 0

uv run governed-eval week3 --dataset tiny --mode fixture --report-path-file artifacts/evals/week3/fixture-report-path.txt
exit 0
```

非 Make 的三个 eval 命令在同一临时 shell 中继承仓库本地只读 `DATABASE_URL`；命令文本本身与 brief 完全一致。未创建或修改 `.env`，未使用网络/live/API client。

### 精确 artifacts 与核心结果

- Week2 summary：`artifacts/evals/week2/week2-fixture-summary.json`
  - run ID `82624b9fa6bc48f3b1a5196baafb25be`
  - suite `ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
  - static safety 20/20（rate 1）
- Week3 pointer：`artifacts/evals/week3/fixture-report-path.txt`
- Week3 report：`artifacts/evals/week3/fixture/20260905T064148Z-week3-20260905T064148Z-2e0d3c8e/report.json`
  - run ID `week3-20260905T064148Z-2e0d3c8e`
  - canonical 40/40；known 30/30；heldout 10/10
  - known behavior 10/10；known simple 15/15；known attribution 1/1
  - candidate first/final result、alias contract、production validation、execution、strict 均为 26/26；first/final truncated occurrence 均 0/26
  - tool 40/40；evidence 27/27；budget 40/40；natural refusal 10/10
  - repair success 1/2（W3K027 success，W3K028 expected no-valid-final）；valid Execute 37/41
  - overall/known/heldout/executed hash 已写入归档分析，不混入 Week2 safety
- 脱敏分析：`docs/reports/week-3-fixture-analysis-2026-09-04.md`

### 停止条件

除宿主机 `make db-up` 端口碰撞外，全部功能与安全离线门禁通过，已达到“可向用户申请一次固定 case/预算 Week3 live 授权”的技术条件；这不是授权。本阶段没有运行 DeepSeek/live/API、没有产生模型费用，提交后停止。

提交前再次运行 `make check`：Ruff、mypy 通过，1641 tests passed in 43.92s。

## Phase B Fix round 1

### RED / mutation

先加入 report leaf 换位、pointer parent rename/recreate、路径 alias 并发和成功 run 后 cleanup 失败回归，在旧实现上运行：

```text
uv run pytest tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py -q
6 failed, 28 passed in 6.21s
```

失败分别证明：`sub/../pointer` 与 canonical path 使用不同线程锁；父目录在 native replace 时被换绑仍返回成功；
Week3 在验证后关闭 report handle，普通文件或 symlink 替换不会阻止 pointer 发布；普通 client/engine cleanup
异常会把已成功生成的不可覆盖 artifact 改判为失败。

### 修复

- pointer parent 现在通过逐组件 `dir_fd` + `O_NOFOLLOW` 打开并持续持有，绑定请求父路径、目录
  `(st_dev, st_ino)` 与规范 target name；线程锁以该三元组为 key，因此 lexical alias 串行。
- 写入在取锁后、replace 前、replace 后和返回前重验“请求父路径 → held directory FD”的 identity。父目录
  rename/recreate 固定失败；post-publication 校验失败时仅在 target 仍为本次 owned inode/bytes 时撤回 pointer。
- Week3 report validation 持有 report directory FD 与 `report.json` FD，读取前后绑定 regular file identity/size，
  从 held FD 解析 JSON 并与 runner artifact 精确比较。pointer replace 前后通过 publication guard 重验 held file、
  leaf directory entry、内容与 report directory 路径绑定；普通文件/symlink 换位均固定脱敏失败且不留下 pointer。
- 成功 runner artifact 不再因普通 client/engine cleanup 异常被改判失败；仍按 client → engine 尽力清理。
  `CancelledError`、`KeyboardInterrupt`、`SystemExit` 在其他资源清理后继续传播，runner primary 仍优先保留。
- 评测文档明确“非 execute 行为用例无数据库调用”，并说明 ProfileTool 会执行受控聚合 SQL；项目计划
  同步到 2026-09-05，标记离线门禁已完成、`db-up` 环境端口碰撞以及 live 尚未授权。

### GREEN / verification

```text
uv run pytest tests/unit/evals/week3/test_cli.py tests/unit/evals/test_week2_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py -q
52 passed in 0.97s

uv run ruff check src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
All checks passed!

uv run mypy src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
Success: no issues found in 4 source files

make check
All checks passed!
Success: no issues found in 158 source files
1651 passed in 43.74s
```

本轮按要求未重跑数据库/eval 门禁，未访问 network/live/API，未修改 production API/SSE 或 ledger。

## Phase B Fix round 2

### RED / mutation

在上一轮实现上新增 held-read 后 leaf 换位、parent fsync 时 report 换位、rollback read/unlink 换位，
以及 `symlink/../pointer` 词法路径回归：

```text
uv run pytest tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py -q
3 failed, 40 passed in 1.12s

uv run pytest tests/unit/evals/test_week2_cli.py -q -k 'rollback_rechecks'
1 failed, 16 deselected in 1.20s
```

前三项失败分别证明旧实现会预先折叠 `..`、在 pointer parent fsync 后缺少最终 report guard、以及
`_ValidatedReport.verify` 读取 held FD 后未再次核对 leaf。单独 rollback mutation 进一步复现：校验函数返回后、
unlink 前换入 foreign inode 时，旧实现会误删 foreign 文件。

### 修复

- 新增单一 handle-bound regular-file verifier：通过 parent `dir_fd` + `O_NOFOLLOW` 打开 leaf，执行
  `fstat before → held-FD read → fstat after → nofollow dir-entry stat`，核对 regular type、dev/ino/size、
  expected bytes，并在返回前再次核对 held FD 与 leaf directory entry。
- pointer 发布后的 content/identity 检查、replace wrapper 模糊状态检查和 rollback 均复用该 primitive；
  rollback 的最终相邻 leaf identity 检查与 unlink 留在 primitive 内，只保留已接受的 native syscall 边界，
  foreign replacement 会保留。
- `_ValidatedReport` 初次读取和每次 publication guard 都复用同一 verifier；每次 held read 后再次验证
  report leaf 与 report parent path/FD identity。atomic pointer 在自身最终 held-FD/dir-entry 验证后、返回前
  再调用一次 publication guard，因此 parent fsync seam 的源换位会失败并撤回 owned pointer。
- 路径构造改为保留用户提供的词法组件，walker 实际逐项打开 `..` 之前的目录。`safe/link/../pointer`
  会在 symlink 处被 `O_NOFOLLOW` 拒绝；真实 `sub/../pointer` 最终打开同一目录 inode，因此 alias lock 仍串行。
- 上一轮 cleanup/cancellation、fixture/live gate、exact artifact 与 CI/Make 断言继续通过；本轮未改变资源所有权语义。

### GREEN / verification

```text
uv run pytest tests/unit/evals/week3/test_cli.py tests/unit/evals/test_week2_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py -q
57 passed in 0.95s

uv run ruff check src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
All checks passed!

uv run mypy src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
Success: no issues found in 4 source files

make check
All checks passed!
Success: no issues found in 158 source files
1656 passed in 42.42s
```

本轮按 controller 要求未重跑 DB/eval，未访问 network/live/API，未修改 production API/SSE 或 ledger。

## Phase B Fix round 3

### RED / mutation

新增 final source/pointer 顺序、report second-parent-walk、temp cleanup helper-return 换位及 partial-write 清理回归：

```text
uv run pytest tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py -q -k 'final_guard or final_checks or partial_temp_cleanup or full_verification_ends'
4 failed, 44 deselected in 1.53s
```

失败证明上一轮仍允许：report 最后 leaf 校验之后执行完整 parent walker；final source guard 换入 foreign pointer 后
无相邻 pointer identity 检查；temp cleanup 在 identity helper 返回后再 unlink，存在误删 foreign replacement 的 seam。

### 修复

- `_ValidatedReport.verify()` 重排为完整 parent path/held-directory 校验在前，held report FD 内容与 leaf
  directory-entry bound verifier 在最后；删除 leaf 后的第二次完整 parent walker。
- 新增只执行 held directory/file 与 leaf identity/type/size 核对的 `verify_identity()`，不重读内容、不打开路径。
  atomic writer 的最终顺序固定为 pointer parent + held-content 完整校验 → report 轻量 final guard → pointer
  held-FD/dir-entry 轻量 identity 校验；最后一步之后不再调用 callback 或执行长读取。
- staging temp 记录创建时 dev/ino；finally 直接调用 handle-bound verifier 的 `unlink_verified=True`，partial write
  不要求 expected bytes/size，因此 owned partial 可清理，foreign replacement 则在相邻身份检查处保留。
- 回归覆盖 second parent helper 不再有执行/换位机会、final guard 主动换 pointer、temp identity helper-return
  换 foreign、owned partial write，以及精确的 full-pointer → light-report → light-pointer 顺序。

report 与 pointer 两组轻量检查之间，以及最终 leaf identity check 与 native syscall/return 之间，仍存在同 UID
对手可调度的两个不可消除原生边界；按既有 Task12 native-syscall trust boundary 接受。本实现不声称提供跨对象原子快照。

### GREEN / verification

```text
uv run pytest tests/unit/evals/week3/test_cli.py tests/unit/evals/test_week2_cli.py tests/unit/test_makefile.py tests/unit/test_ci_workflow.py -q
63 passed in 0.86s

uv run ruff check src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
All checks passed!

uv run mypy src/governed_analytics/evals/cli.py src/governed_analytics/evals/week3/cli.py tests/unit/evals/test_week2_cli.py tests/unit/evals/week3/test_cli.py
Success: no issues found in 4 source files

make check
All checks passed!
Success: no issues found in 158 source files
1662 passed in 42.47s
```

本轮未重跑 DB/eval，未访问 network/live/API，未修改 production API/SSE 或 ledger。
