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
- SQL statement classifier 对 `SELECT`/`WITH`/`VALUES` 复用生产 `validate_sql()` 的解析/拒绝码，对 DML/DDL/GRANT/COPY 等使用完整命令结构；普通说明文本（包括自然语言中的 `select ... from ...`）保持可发布。
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
