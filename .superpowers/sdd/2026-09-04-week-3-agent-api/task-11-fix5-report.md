# Task 11 Fix Round 5 报告

## 范围

- 基线：`22502d2b2026d405a2bac12de43dac7b2cc53473`
- 修改范围仅限 `tests/integration/api/test_api_sse.py` 与本报告。
- 未修改生产代码、Task 11 其他测试、控制器 ledger；未进行 live、网络或拉镜像操作。

## RED 证据

先新增回归测试，再修改测试 harness/oracle。目标用例命令：

```text
uv run pytest tests/integration/api/test_api_sse.py -q \
  -k 'sse_parser_accepts_real or safe_output_oracle_scans or await_deadline_rejects or close_deadline_uses or tracked_close_probe_requires'
```

修正纯内存测试夹具后，现有 harness 的可复现失败如下：

- 真实 `ServerSentEvent(data=json.dumps(..., indent=2), sep="\r\n")` 的第二个 `data:` 被拒绝。
- 合法 `run.created` envelope 的 `data.status` 中 `token`、`secret`、`credentials` 和通用 SQL 未被安全 oracle 拒绝。
- 吞掉 `CancelledError` 并在 5 倍 deadline 后返回的任务被 `_await_with_deadline` 接受为 late success。
- `_close_with_deadline` 对相同预算连续等待，耗时超过单一截止上界。
- `_tracked_close_failure_probe` 只返回 lease 数，无法证明确实观察到 `RuntimeError`。

上述 RED 均为预期的行为失败；失败诊断保持固定消息，敏感 operand 未进入参数化 ID 或断言输出。

## GREEN 实现

- SSE parser 按到达顺序保存字段；`data` 允许出现一次或多次并用 `\n` 拼接后解析 JSON；`id`、`event` 各要求恰好一次。
- 保持未知字段、`retry`、重复/缺失 `id`/`event`、缺失 `data`、event/envelope mismatch 为 fail-closed；覆盖 CRLF 经 `splitlines()`/`aiter_lines()` 形态、冒号后无空格、comment/heartbeat 和连续空 frame。
- 对完整 raw wire 和每一行扫描，并对拼接后的 JSON 树全部字符串叶扫描；credential assignment 扩展到 `token`、`secret`、`credentials`，SQL 检测扩展到 SELECT/FROM、WITH CTE、INSERT、UPDATE、DELETE 及常见 DDL，保留自然语言 `select the best evidence`。
- `_await_with_deadline` 使用 `loop.time()` 绝对截止与独立 task 竞速；到期后立即固定消息失败，不等待目标响应取消。hostile task 测试在到期时确认其仍 pending，随后释放私有 gate 并 `gather` 回收，无残留 task/lease。
- `_close_with_deadline` 复用单一预算，不再进行约三倍串行等待。
- close probe 返回 `runtime_error_observed` 与 `active_leases` 的结构化结果，并以正常 close 负向 mutation-style probe 证明未观察到异常时不能误报通过。

## 验证结果

### Task 11 正常、反序与严格轮次

1. 正常序：

   ```text
   DATABASE_URL=<本地只读 DSN> uv run pytest \
     tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -v
   ```

   结果：`9 passed in 1.71s`。

2. 反序：

   ```text
   DATABASE_URL=<本地只读 DSN> uv run pytest \
     tests/integration/api/test_api_sse.py tests/integration/agent/test_graph.py -v
   ```

   结果：`9 passed in 1.66s`。

3. asyncio debug + warnings-as-errors：

   ```text
   PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error DATABASE_URL=<本地只读 DSN> \
     uv run pytest --asyncio-debug \
     -o asyncio_default_fixture_loop_scope=function \
     tests/integration/agent/test_graph.py tests/integration/api/test_api_sse.py -v
   ```

   结果：pytest 标头确认 `debug=True`，`9 passed in 1.86s`。

说明：首次直接使用全局 `PYTHONWARNINGS=error` 时，pytest 在收集前因仓库既有的 `asyncio_default_fixture_loop_scope` 未设置弃用告警而中止；严格轮次通过命令行显式设为 `function` 后运行，未修改项目配置。

### 旧集成与 fixture 回归

```text
DATABASE_URL=<本地只读 DSN> uv run pytest tests/integration/tools tests/integration/metrics -v
```

结果：`42 passed in 1.65s`。

```text
DATABASE_URL=<本地只读 DSN> uv run governed-eval baseline --dataset tiny --mode fixture
DATABASE_URL=<本地只读 DSN> uv run governed-eval week2 --dataset tiny --mode fixture
```

结果：两条命令均 exit 0，无 tracked 文件变化；baseline dataset ID 保持 `a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`，Week2 suite manifest SHA-256 保持 `ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`，70 个 Week2 case。

### 静态与全仓门禁

```text
uv run ruff check tests/integration/api/test_api_sse.py
uv run mypy tests/integration/api/test_api_sse.py
make check
```

结果：ruff 通过；mypy 通过；`make check` 完成静态检查并通过 `1172 passed in 22.85s`。

## 风险与边界

- asyncio 不能强制终止永久忽略取消的 coroutine；本 harness 的承诺是硬截止后立即失败并观察 pending，受控 probe 再释放私有 gate、等待回收。真实调用方仍需拥有可释放的资源/gate。
- SQL oracle 是面向序列化输出泄漏的保守语句形状检测，不是 SQL parser；已覆盖本轮要求的常见 DML/DDL，同时以合法自然语言回归约束误报。
- 新增 5 个回归为纯内存测试；原有 4 个 Task 11 真实 DB 测试（Agent 3 个、API 1 个）保持不变并全部通过。
