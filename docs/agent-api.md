# Agent API 本地使用指南

第三周提供单进程 FastAPI/SSE 后端。先在仓库根目录按 README 完成依赖、迁移、tiny 数据及 `make data-verify`。
以下命令显式使用 fixture 模式，不发起模型 API 调用，也不需要模型 Key。

## 启动与健康检查

```bash
AGENT_RUNTIME_MODE=fixture AGENT_LIVE_ENABLED=false \
  uv run uvicorn governed_analytics.api.app:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

两者均返回 `{"status":"ok"}`。`readyz` 检查容器已初始化，不代替数据库数据核验；成功的完整分析才证明查询链路可运行。

## 创建任务与读取结果

```bash
curl --fail -X POST http://127.0.0.1:8000/v1/analyses \
  -H 'Content-Type: application/json' \
  -d '{"query":"2026年6月GMV是多少？"}'
```

返回 HTTP 202，包含 `run_id`、`status_url`、`events_url`、`trace_url`。将下面的 `RUN_ID` 替换为本次返回值：

```bash
curl --fail http://127.0.0.1:8000/v1/analyses/RUN_ID
curl --fail -N http://127.0.0.1:8000/v1/analyses/RUN_ID/events
curl --fail http://127.0.0.1:8000/v1/analyses/RUN_ID/trace
```

状态从 queued/running 进入 terminal。成功终态包含 `final_status=completed`、回答、带 `query_id` 的数值证据及停止原因。
运行中的 Trace 是 `snapshot_complete=false` 初始快照，实时进度使用 SSE；结束后 Trace 是冻结终值。

fixture API 只支持以下两条内建脚本（保留标点和窗口）：

1. `2026年6月GMV是多少？`
2. `比较 2026-06-01 至 06-08 与 06-08 至 06-15 的 GMV，并按区域、SKU、客户分群解释下降。`

其他问法返回 unsupported。脚本用于验证工具、数据库、HTTP 和事件链路，不能代表模型理解任意问题。
它与 `make eval-week3-fixture` 的 40 例评测脚本库分别装配，不会通过 API 加载 Oracle。

## SSE 重放

每个事件有递增 sequence、`id`、`event` 与 JSON `data`；每个任务只有一个 `run.terminal`。
断开 SSE 不取消分析。将已经收到的完整事件 ID 放入 `Last-Event-ID`，只重放该事件之后的内容：

```bash
curl --fail -N \
  -H 'Last-Event-ID: RECEIVED_EVENT_ID' \
  http://127.0.0.1:8000/v1/analyses/RUN_ID/events
```

使用实际返回的 ID，不自行拼接。非法游标返回 400、超前游标 409、未知任务 404。

## 当前范围

- RunStore/EventStore 在内存中，进程重启后状态丢失；不要使用多 worker 来共享这些任务。
- 本地监听 loopback；第三周不提供身份认证或公网部署验收。
- Trace/SSE 只暴露受控字段，不包含 Prompt、SQL 参数、原始数据行和 Oracle。
- fixture 调用的 token 与模型费用为零；live 模式是独立配置和明确授权的执行方式，本指南不启用它。
- 停止 Uvicorn 使用 Ctrl-C，由应用生命周期关闭 runner 与数据库 engine。

2026-09-07 已通过本机 Uvicorn 的两条完整 HTTP 演示和 Last-Event-ID 重放，详见
[第三周交付记录](reports/week-3-closeout-2026-09-07.md)。
