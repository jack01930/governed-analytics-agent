# Task 9 Fix 5 报告

## 状态

DONE

## 修复摘要

- `RunStore` 与 `EventStore` 增加兼容的可选 `owner_token` 参数。`AnalysisRunner` 在每次 submission 的任何 create 之前生成一个不可序列化、不可猜测、仅进程内按 identity 比较的 opaque object，并在两个 store 间共享该 token。
- `InMemoryRunStore` 在同一把 store lock 内绑定 record generation 与 token；带 token 的 get、状态迁移和条件删除都先做 exact identity 校验。`RunRecord` 公共 Pydantic 模型未增加 token 字段。
- `InMemoryEventStore` 在 registry lock 与该 generation 的 condition lock 同时覆盖的临界区内校验 token；create、emit、terminal emit、high-water、snapshot、terminal read 和 delete 都支持 exact generation 条件。token 不进入 `RunEvent`、SSE data 或公共序列化。
- 普通 store caller 与 SSE read path 仍可省略 token，保持 Task 8 公共接口兼容；Runner 的提交、`run.created`、执行生命周期、有限 read-back、rollback、restore 和自有 terminal prune 路径始终传 exact token。
- ownership mismatch 使用稳定的 `RunOwnershipMismatch` / `EventOwnershipMismatch`，消息不含 token。pre-side-effect 失败后出现 foreign generation 时，Runner 不删除、不写入该 generation，且进入稳定 fail-stop；同 token 的 post-side-effect 失败仍能通过有限 read-back 清理收敛。
- restore compensation 的 create、high-water、`run.created` emit 与 snapshot proof 都绑定原 submission token。owned buffer 删除后若 foreign replacement 抢占相同 ID，restore 只得到 mismatch，不会向 replacement 写事件。
- `_DelayedCancellation` 现在在初始化、dependency await 前、正常返回后和异常出口采样父任务 `Task.cancelling()`。依赖吞掉 `CancelledError` 正常返回、普通异常或 `BaseExceptionGroup` 覆盖取消时，都会保留 pending cancellation；若依赖显式 `uncancel()` 至 0，则清除仅由 cancelling count 合成的 pending，不伪造取消。
- `_prune_locked()` 的首次 `expired_terminal_ids()` 读取也使用同一 coordination helper。read-only preflight 被取消时先安全退出；清理阶段被取消时完成有限 reconciliation 或进入 fail-stop 后再传播取消。

## TDD 证据

### RED

生产实现修改前先加入 6 个确定性 ownership / cancellation 探针并运行：

```text
uv run pytest tests/unit/runtime/test_runs.py -k 'foreign_generation or foreign_replacement or swallowing_cancellation or prune_expired_ids_cleanup' -q
```

结果：`4 failed, 2 passed, 69 deselected`。

- RunStore pre-side-effect create failure后出现 foreign generation：旧实现把 foreign `RunRecord` 当成本提交 owned record 删除，并错误返回 `RunSubmissionFailed`。
- EventStore 对 foreign empty / nonempty generation：旧实现按“调用前不存在、失败后存在”推断 owned，删除 foreign buffer，并错误返回 `RunSubmissionFailed`。
- prune 的首个 `expired_terminal_ids()` 在取消被 finally child 的 `RuntimeError` 覆盖时：旧实现把公开结果转换成 `RunConsistencyError`。
- 首轮 restore / swallowed-cancel 探针的 barrier 尚未卡在最后 read/restore replacement 窗口，旧实现意外通过；随后收紧为“owned event 已确认删除后、restore 前插入 replacement”以及“restore replay 吞取消后正常返回”，并增加 exact-token store 单测与 `uncancel()` 对照。

### GREEN

- Task 8 + 9 focused：`158 passed`（1.40s）。
- Runtime + Agent：`426 passed`（8.30s）。
- `uv run ruff check .`：通过。
- `uv run mypy`：128 source files，无问题。
- `git diff --check`：通过。
- `make check`：Ruff/mypy 通过，`1121 passed`（21.31s；基线 1111 + 新增 10）。

`tests/integration` 当前直接运行结果为 `18 passed, 50 failed`；失败均为当前 shell 未提供 `DATABASE_URL` / `LOADER_DATABASE_URL` 等数据库前置条件或本地数据库执行不可用。Task 8/9 没有独立数据库 integration suite，未启动 Docker、未发起外部网络请求、未执行 live 或 API 调用。

## 新增确定性覆盖

- RunStore foreign generation：foreign query/status 原样保留，无本提交 executor，Runner 后续稳定拒绝。
- EventStore foreign empty/nonempty generation：HWM 与内容原样保留，owned RunRecord 收敛删除，无 executor，后续稳定拒绝。
- restore replacement generation：foreign HWM 保持 0，未补写 `run.created`，owned RunRecord 保留配对补偿语义，Runner 稳定 fail-stop。
- exact token 条件 get/high-water/emit/delete；wrong token 只返回稳定 mismatch，generation 不变。
- dependency 吞取消正常 return；restore replay 末端吞取消；显式 `uncancel()` 回 0 对照；prune expired-ID masked cancellation。
- 既有 post-side-effect create/delete、重复 reconciliation、finite snapshot、terminal 唯一性、capacity/TTL/active SSE、shutdown 与 fail-stop 线性化测试全部保持通过。

## 修改范围

- `src/governed_analytics/runtime/events.py`
- `src/governed_analytics/runtime/runs.py`
- `tests/unit/runtime/test_events.py`
- `tests/unit/runtime/test_runs.py`
- `.superpowers/sdd/2026-09-04-week-3-agent-api/task-9-fix5-report.md`

未修改 controller ledger，未进入 Task 10，未新增依赖，未发起外部网络请求，未执行 live 或 API 调用。

## 兼容性与风险

- 省略 token 保留 Task 8/9 的既有公共行为；这条兼容路径不提供 generation ownership 保证。Runner 的协调路径不使用该弱路径。
- token 是进程内 identity capability，不支持跨进程恢复；第三周设计本就明确不实现持久化 checkpoint 或跨进程恢复。
- 两个内存 store 之间没有分布式事务。实现通过 exact generation 条件 mutation、有限 read-back/restore 与稳定 fail-stop 收敛已授权窗口；无法确认跨 store 一致性时不会继续接单。
