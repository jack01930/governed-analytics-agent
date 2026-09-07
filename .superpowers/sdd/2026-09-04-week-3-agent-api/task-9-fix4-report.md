# Task 9 Fix 4 报告

## 状态

DONE

## 修复摘要

- 提交回滚现在通过 `_SubmissionEventState` 显式携带 EventStore 的 `owned` 与 `deleted` 状态。只有本次提交创建、且回滚已确认删除的 EventStore buffer，才会在 owned queued RunRecord 无法删除时进行配对恢复。
- EventStore 预置同一 `run_id` 时，无论 HWM 为 0 还是已有事件，create collision 后都不会删除或补写该非本次拥有的 buffer；若 owned RunRecord 无法清理，runner 进入稳定 fail-stop，后续 submit 固定拒绝。
- `_DelayedCancellation.observe()` 现在同时依据父任务 `Task.cancelling()` 与捕获异常判断取消意图。依赖清理以 `RuntimeError` 等普通异常覆盖 `CancelledError` 时，仍会延迟保存取消语义；底层异常仅作为最终 `CancelledError` 的诊断 cause。
- `_DelayedCancellation.await_dependency()` 作为集中 await 边界，覆盖协调阶段的 EventStore create/delete/high-water/replay/emit，以及 RunStore delete/get。有限 reconciliation 完成或稳定 fail-stop 后才最终传播取消。
- reviewer 代表探针覆盖：`run.created` emit 失败进入 rollback；EventStore delete 已生效后阻塞；父 submit 被取消；finally 启动并 await 一个抛 `RuntimeError` 的 child 覆盖取消。最终两存储均无残留、child 已完成、submit task 为 cancelled，且 runner 能继续完成下一单。
- 参数化矩阵额外覆盖 RunStore get、EventStore high-water、restore create、restore emit 与 restore replay 的同类取消覆盖窗口。

## TDD 证据

### RED

生产实现修改前运行新增 scoped 测试：

```text
uv run pytest tests/unit/runtime/test_runs.py -k 'failed_rollback_never_restores or cleanup_exception or readback_cleanup_exception or restore_cleanup_exception' -q
```

结果：`7 failed, 1 passed, 61 deselected`。

- 预置空 EventStore 的 HWM 被错误地从 0 改为 1；预置非空对照保持不变。
- rollback delete、普通 get/high-water read-back、restore create/emit/replay 的六个取消窗口均把公开结果错误转换为 `RunSubmissionFailed` 或 `RunConsistencyError`。

### GREEN

- 同一新增 scoped 命令：`8 passed, 61 deselected`（0.39s）。
- `uv run pytest tests/unit/runtime/test_events.py tests/unit/runtime/test_runs.py -q`：148 passed（1.35s）。
- `uv run pytest tests/unit/runtime tests/unit/agent -q`：416 passed（8.21s）。
- `uv run ruff format --check src/governed_analytics/runtime/runs.py tests/unit/runtime/test_runs.py`：2 files already formatted。
- `uv run ruff check .`：通过。
- `uv run mypy`：128 source files，无问题。
- `git diff --check`：通过。
- `make check`：Ruff/mypy 通过，1111 tests passed（21.46s）。

## 修改范围

- `src/governed_analytics/runtime/runs.py`
- `tests/unit/runtime/test_runs.py`
- `.superpowers/sdd/2026-09-04-week-3-agent-api/task-9-fix4-report.md`

未修改 `.superpowers/.../progress.md` controller ledger，未进入 Task 10，未执行网络、live 或 API 调用。

## 风险

- 当有限回读无法确认副作用状态时，仍按既有 ruling 进入稳定 fail-stop；若父任务已有取消意图，公开结果保持 `CancelledError`，而不是暴露底层依赖异常。
- 取消被依赖清理异常覆盖后，Python 已无法取回原始 `CancelledError` 对象；实现依据 `Task.cancelling()` 重建等价取消传播，并保留覆盖异常为诊断 cause。对调用者可观察语义仍是 task `cancelled() == True`。
