# Task 12 Fix Round 2 报告

日期：2026-09-05
基线：`faddf3711dd81d0115affbe21f5e31bc562d4189`

## 修复范围

- 对 truth scanner 的所有字符串输入先执行 Unicode NFKC 规范化和
  `casefold()`；从 `_TRUTH_KEYS` 逐项生成完整敏感短语模式。
- 敏感短语匹配要求前后为非字母数字边界，并允许字母数字之间出现 Unicode
  标点、空白、格式字符和下划线；不再使用 collapse 后的裸 substring。
- 对 mapping key/value、`model_purpose`、raw candidate SQL 和 SQL 注入后的
  expanded action 全部执行相同扫描；仅精确放行 `expected_evidence` key。
- staging 创建后立即记录父目录和 staging 的 `lstat(st_dev, st_ino,
  st_mode)` 身份；发布前验证原路径仍绑定到同一个 0700 regular directory，
  发布后验证 final path 仍是同一身份。
- 异常清理只删除路径和父目录身份均仍匹配的 owned staging。已被换绑的
  foreign path 与被移走的 owned alias 均不删除。
- `FrozenExpectedResult` 递归拒绝所有非有限 `float`/`Decimal`；因此除 JSON
  常量外，`1e9999` 解析所得的正无穷也会 fail closed。

## RED

命令：

```text
uv run pytest -q tests/unit/evals/week3/test_suites.py \
  tests/unit/evals/week3/test_models.py \
  tests/unit/evals/week3/test_fixtures.py
```

结果：`12 failed, 70 passed`。失败覆盖 NFKC 全宽伪装、敏感词内部插入分隔符、
`unexpected rows` 边界误报、`1e9999`、六类递归非有限数，以及两类 staging
source-swap。旧实现已能拒绝普通 snake/spaces/hyphen/camel/zero-width 中的部分
形式，因此这些形式在 RED 时已通过，但仍纳入最终回归矩阵。

## GREEN

```text
uv run pytest -q tests/unit/evals/test_week2_frozen_protocol.py tests/unit/evals/week3
84 passed in 13.27s

uv run ruff check src/governed_analytics/evals/week3 tests/unit/evals/week3
All checks passed!

uv run mypy src/governed_analytics/evals/week3 tests/unit/evals/week3
Success: no issues found in 8 source files

make check
All checks passed!
Success: no issues found in 149 source files
1256 passed in 36.41s
```

truth isolation 测试矩阵覆盖 7 个 `_TRUTH_KEYS` × mapping key、mapping value、
purpose、raw SQL 四个 surface，并覆盖 snake、spaces、hyphen、camel、fullwidth、
zero-width、词内分隔符，以及 expanded action 调用和 `unexpected rows` 负例。

staging 测试覆盖：

- publish 前换绑：destination 不存在，foreign 原路径和 owned alias 均保留；
- destination collision 前换绑：collision destination、foreign 原路径和 owned
  alias 均保留，清理不删除 foreign。

## Oracle 重算

使用本地 tiny PostgreSQL 和显式 `analytics_readonly` 连接，在仓库内同父临时
目录执行 freezer。结果：

```text
oracle_recompute_files=19 bytes_match=true second_freeze=rejected staging=clean
```

19 个 Oracle 结果均与已冻结 expected JSON 逐字节一致；本轮未修改 Oracle SQL、
expected JSON 或 registry。

## Hash

- Week 2 frozen：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- Week 3 overall：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- Week 3 known：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- Week 3 heldout：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`

本轮只修改实现、测试与报告，所有 protocol manifest hash 保持不变。

## 风险与未决项

- 身份校验检测已经发生的路径换绑，并在失败时保留 foreign path。它不宣称能
  消除同 UID 恶意进程在身份校验与系统调用之间每一个纳秒级竞态；真正发布仍由
  平台原子 no-replace primitive 保证 destination 不被替换。
- 检测到换绑时，为避免误删，owned alias 和 foreign path 会被保留，需由隔离的
  调用环境负责后续取证或清理。
- 无其他未决项；未修改 controller ledger 或 Task11 SSE 测试。
