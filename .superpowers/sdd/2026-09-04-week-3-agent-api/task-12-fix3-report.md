# Task 12 Fix Round 3 报告

日期：2026-09-05
基线：`842290a773421d4e25e27289477707487dfe330a`

## 修复范围

- truth pattern 在 NFKC + `casefold()` 后，把内部所有
  `not character.isalnum()` 字符视为可跳过分隔符，包括 Unicode `M*`、`Cf`、
  variation selector、CGJ、zero-width、标点、空白和下划线。
- 敏感短语整体仍使用字母数字边界；`unexpected rows` 不会误匹配，合法精确
  `expected_evidence` 继续放行。
- 引入 `_OwnedStaging`，持有 parent/staging 的 no-follow directory fd、路径和
  `lstat`/`fstat` 身份。handle 生命周期覆盖 freeze、publish、cleanup，最终在
  `finally` 中关闭。
- publisher 不再接受未绑定 raw staging path。统一 native publisher 在原子
  no-replace 调用前核验 parent path、parent fd、staging path、staging fd 的
  dev/inode/mode。
- 原生调用后以仍打开的 staging fd 和 destination `lstat` 核验发布身份。若原生
  seam 期间发生换绑并错误发布 foreign directory，则将该目录原子 no-replace
  移入唯一 `.week3-quarantine-*`，保持最终 destination 不可见，并保留 foreign
  内容供取证。
- cleanup 只接受 `_OwnedStaging`，在 cleanup 入口和实际删除 helper 内部均重新
  核验 parent/path/fd 身份；换绑时直接返回，不删除 replacement 或 owned alias。

## RED

命令：

```text
uv run pytest -q tests/unit/evals/week3/test_suites.py \
  tests/unit/evals/week3/test_fixtures.py
```

结果：`111 failed, 72 passed`。其中 105 个失败来自 7 个 `_TRUTH_KEYS` × 5 个
surface × 3 类新增 Unicode mutation；其余失败覆盖 fixed redacted error 和三个
新增 staging ownership seam。

## GREEN

```text
uv run pytest -q tests/unit/evals/test_week2_frozen_protocol.py tests/unit/evals/week3
195 passed in 13.49s

uv run ruff check src/governed_analytics/evals/week3 tests/unit/evals/week3
All checks passed!

uv run mypy src/governed_analytics/evals/week3 tests/unit/evals/week3
Success: no issues found in 8 source files

make check
All checks passed!
Success: no issues found in 149 source files
1367 passed in 36.66s
```

truth mutation 覆盖以下每一种组合：

- 7 个 `_TRUTH_KEYS`；
- mapping key、mapping value、purpose、raw candidate SQL、expanded action；
- U+034F COMBINING GRAPHEME JOINER、U+FE0F VARIATION SELECTOR-16，以及由
  U+2060/U+034F/U+FE0F/U+200B 组成的混合 default-ignorable 序列。

每个入口仍返回固定脱敏错误。已有 snake/spaces/hyphen/camel/fullwidth/
zero-width 以及 `unexpected rows`/`expected_evidence` 回归测试继续通过。

staging deterministic tests 覆盖：

- controller 已创建并绑定 handle 后、publisher 入口前发生 source swap；
- publisher 内部身份校验后、native no-replace seam 前发生 source swap；
- cleanup 入口前发生 source swap；
- cleanup 完成首轮检查后、实际删除 helper 入口前发生 source swap。

所有换绑场景均保留 foreign 和 owned alias；错误发布场景将 foreign 保留在唯一
quarantine，最终 destination 不存在。正常 freeze 失败仍清理 owned staging，正常
成功与 destination no-replace 行为不回归。

## Oracle 重算

使用本地 tiny PostgreSQL、显式 `analytics_readonly` 连接和仓库内同父临时目录
执行 freezer：

```text
oracle_recompute_files=19 bytes_match=true second_freeze=rejected staging=clean
```

19 个结果与冻结 expected JSON 逐字节一致。本轮未修改 Oracle SQL、expected JSON
或 registry。

## Hash

- Week 2 frozen：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- Week 3 overall：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- Week 3 known：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- Week 3 heldout：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`

本轮只修改实现、测试和报告，protocol manifest hashes 保持不变。

## 可信边界与风险

- `_OwnedStaging` 检测已发生的路径换绑，并通过 fd 维持原目录身份；平台原子
  no-replace primitive 防止覆盖 destination。
- 本实现不声称抵御同 UID 恶意进程在身份检查和 native syscall 之间的每一个
  纳秒级竞态。若 postcheck 发现错误身份，协议边界是原子隔离错误 destination，
  保留 quarantine 内容而非删除 foreign。
- 缺少 no-follow directory-open 能力的平台会 fail closed，不降级为不绑定的 raw
  path 发布。
- 无其他未决项；未修改 controller ledger 或 Task11 SSE 测试。
