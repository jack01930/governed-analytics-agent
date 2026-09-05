# Task 12 Fix Round 1 报告

日期：2026-09-05
基线：`da6524923226fef59e477b86e651a801f1f70ace`

## 修复范围

- freezer 终态发布改为 Darwin `renamex_np(RENAME_EXCL)`、Linux
  `renameat2(RENAME_NOREPLACE)`、Windows `MoveFileExW` 的原子 no-replace
  实现；发布边界发生目标目录竞态时保留竞态方目录，并清理本次 staging。
- freezer 在建立数据库连接前，对 Oracle root、所有路径组件、19 个 leaf
  及 exact inventory 执行 no-symlink/regular-file 检查。
- `BudgetOverrides` 仅在两个上限均显式提供时交叉比较；W3K029 仅下调
  `max_tool_calls: 3`，终态固定为 `partial/evidence_partial`。
- `Week3RunReport.protocol_version` 固定为
  `week3-agent-evaluation-v1`。
- truth isolation 的字符串 token 全部从 `_TRUTH_KEYS` 规范化派生，扫描 raw
  candidate SQL 和注入 SQL 后的 expanded action；精确放行合法
  `expected_evidence`。
- strict expected JSON 通过 `parse_constant` 拒绝 `NaN`、`Infinity` 和
  `-Infinity`，统一返回脱敏错误。

## RED

命令：

```text
uv run pytest -q tests/unit/evals/week3/test_models.py \
  tests/unit/evals/week3/test_suites.py \
  tests/unit/evals/week3/test_fixtures.py
```

结果：`14 failed, 29 passed`。14 个失败分别覆盖单字段 budget、protocol
literal、W3K029 terminal contract、两个遗漏 truth token、三个非有限 JSON
常量、原子发布竞态、三类 Oracle symlink 和 orphan inventory。

## GREEN

```text
uv run pytest -q tests/unit/evals/test_week2_frozen_protocol.py tests/unit/evals/week3
45 passed in 10.73s

uv run ruff check .
All checks passed!

uv run mypy src tests/unit/evals/week3
Success: no issues found in 82 source files

make check
All checks passed!
Success: no issues found in 149 source files
1217 passed in 33.42s
```

本地 tiny PostgreSQL Oracle 重算命令使用显式只读连接，并把输出写入仓库内
临时父目录。结果：`oracle_recompute_files=19 bytes_match=true
second_freeze=rejected staging=clean`。

## 19 个冻结结果

```text
W3K011.json
W3K012.json
W3K013.json
W3K014.json
W3K015.json
W3K016.json
W3K017.json
W3K018.json
W3K019.json
W3K020.json
W3K021.json
W3K022.json
W3K023.json
W3K024.json
W3K025.json
W3K026-confirm_decline.json
W3K026-region_contribution.json
W3K026-sku_contribution.json
W3K026-segment_contribution.json
```

19 个结果均与已提交 expected bytes 一致，本轮未改动 Oracle SQL 或 expected
JSON。

## Hash

- Week 2 frozen：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- Week 3 overall：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- Week 3 known：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- Week 3 heldout：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`

overall/known/heldout 因 W3K029 registry contract 修正按设计变化；Week 2
保持 byte-frozen。

## 风险与未决项

- 严格 no-symlink 输出规则会拒绝 macOS `/var`（指向 `/private/var`）下的默认
  `TemporaryDirectory`；验证改用仓库内真实目录，生产默认 expected 路径不受影响。
- `max_execute_calls` 留空后的基础预算合并与 `tool_call_limit` trace 判定属于
  Task 13 scorer/runner，本轮按批准设计不提前实现。
- 无其他未决项；未修改 controller ledger 或 Task11 SSE 测试。
