# Task 12 Fix Round 4 报告

日期：2026-09-05
基线：`d8e9f62ac6e84bf263f50ea447ec1864a5eed040`

## 修复范围

- 真值文本改为按原始 code point 分别执行 NFKC + `casefold()`，不再先对整串
  NFKC。显式 U+0300/U+0301 等 combining mark 因而不会与前一 ASCII 字母合成，
  会继续作为 pattern 内部的非字母数字 separator 被跳过。
- 既有 CGJ、variation selector、zero-width、标点、空白和下划线隔离继续生效；
  整体前后字母数字边界不变。真正的 precomposed accented letter 仍保留为字母，
  不会把普通变音词扩散成敏感 phrase；`unexpected rows` 与精确
  `expected_evidence` 回归继续放行。
- `_OwnedStaging` 为 directory fd 与 parent fd 分别维护成功关闭状态；每次
  `close()` 都尝试所有尚未成功关闭的 fd，优先传播本次首个 `OSError`，成功项
  不重复关闭，失败项可在后续调用恢复，完整成功后重复 close 幂等。
- staging handle 构造失败 rollback 使用嵌套 `try/finally`：directory fd close
  无论成功或失败都继续尝试 parent fd；二者的异常都不会覆盖原构造异常；路径
  identity 清理位于最外层 `finally`，不会被任一 close 异常截断。

本轮只修改 Task 12 的 `suites.py`、`fixtures.py`、两份对应 unit test 和本报告；
未修改 controller ledger、Task 11、registry、candidate/Oracle SQL 或 expected JSON，
未访问外部网络或 live 模型。

## TDD：RED

Unicode focused：

```text
uv run pytest -q tests/unit/evals/week3/test_suites.py \
  -k 'skips_all_non_alnum or precomposed_accented or unicode_marks_fail'

72 failed, 115 passed, 60 deselected
```

其中 70 个核心失败来自 7 个 `_TRUTH_KEYS` × 5 个 surface ×
U+0300/U+0301；另 2 个失败证明 raw candidate SQL 没有返回固定脱敏错误。
precomposed accented word 的负向边界在旧实现下保持通过。

FD focused：

```text
uv run pytest -q tests/unit/evals/week3/test_fixtures.py \
  -k 'owned_staging_close or owned_staging_creation_rollback'

6 failed
```

first/second/both `os.close` 注入证明旧实现会在首错后跳过另一个 fd、没有逐 fd
状态，并会在构造 rollback 中用 close error 覆盖原异常且截断 identity cleanup。

## GREEN 与静态门禁

```text
Unicode focused: 187 passed, 60 deselected
FD focused: 6 passed, 15 deselected

uv run pytest -q tests/unit/evals/test_week2_frozen_protocol.py tests/unit/evals/week3
280 passed in 13.93s

uv run ruff check src/governed_analytics/evals/week3 tests/unit/evals/week3
All checks passed!

uv run mypy src/governed_analytics/evals/week3 tests/unit/evals/week3
Success: no issues found in 8 source files

make check
All checks passed!
Success: no issues found in 149 source files
1452 passed in 35.58s
```

FD 故障测试对首个、第二个和两个 close 同时失败分别注入不同位置，断言两个 fd
都被尝试、首错稳定传播、成功/失败状态精确。测试随后恢复真实 `os.close`，仅对
`fstat` 仍开放的真实 fd 做安全回收，并验证 `/dev/fd` 前后计数相同；没有测试
遗留泄漏。构造 rollback 同时验证 owned staging identity 已清理。

## Oracle 19、hash 与 SQL

使用本地 tiny PostgreSQL 和显式 `analytics_readonly` 配置，在仓库内自动回收的
临时同父目录重算：

```text
oracle_recompute_files=19 bytes_match=True second_freeze_rejected=True staging_clean=True
```

- Week 2 frozen：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- Week 3 overall：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- Week 3 known：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- Week 3 heldout：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`

`git diff --quiet -- evals/datasets/week3` 通过，协议 hash、全部 SQL 与 expected bytes
均未改变。

## 结论

两项本轮 finding 均已关闭，无新增未决项。逐 code point 规范化在兼容字符支持、
combining/default-ignorable 绕过防护和普通变音词边界之间保持明确区分；逐 fd 状态
与外层 rollback cleanup 保证任一 close 失败不会永久阻断另一个 fd 或 staging 清理。
