# Task 12 Fix Round 5 报告

日期：2026-09-05
基线：`9eeb28505d58d56aaa071350913b329ef7aa7396`

## 修复范围

- 真值规范化改为来源感知的结构化 `_TruthUnit` 序列，并显式区分
  `literal`、`separator`、`barrier` 三种类型。
- 原始 code point 是 alnum 时，仍逐字符执行 NFKC + `casefold()`，兼容
  fullwidth、compatibility 与大小写折叠；展开所得 alnum 是 literal，展开所得
  mark/标点是 hard barrier，matcher 绝不跳过。
- 原始 code point 是非 alnum 时保留为可跳过 separator。因此攻击者显式插入
  U+0300、U+0301、U+034F、U+FE0F、zero-width 的既有检测不变。
- hard barrier 同时保留原 alnum 的前后边界语义，避免在展开单元中间错误建立
  pattern 边界。未采用整串 NFD 或无来源地删除 mark。

本轮只修改 `suites.py`、对应 unit test 和本报告；未修改/stage controller
ledger、Task 11、`fixtures.py` 的 FD 逻辑、registry、candidate/Oracle SQL、expected
JSON 或任何数据，也未访问网络或 live 模型。

## TDD：RED

先加入 7 个 `_TRUTH_KEYS` × 5 个 surface（mapping key/value、purpose、raw SQL、
expanded action）的 casefold-mark alnum 负例矩阵：

```text
uv run pytest tests/unit/evals/week3/test_suites.py -k 'casefold_mark' -q
35 failed, 251 deselected
```

35 个失败全部是旧 matcher 的误报。矩阵覆盖：

- `expected_result_path` → `expecẗedresultpath`
- `expected_rows` → `expectedroẘs`
- `expected_sql` → `expecẗedsql`
- `oracle_sql_path` → `oracŀesqlpath`
- `oracle_query_id` → `oraclequeryİd`
- `scorer_truth` → `scorertrutẖ`
- `ground_truth` → `groundtrutẖ`

其中 review 指定的四个字符串全部包含在矩阵和独立具名回归中。

## GREEN 与边界回归

```text
source-aware focused: 40 passed, 247 deselected
separator/boundary/fullwidth-compatible regression: 196 passed, 91 deselected

uv run pytest -q tests/unit/evals/test_week2_frozen_protocol.py tests/unit/evals/week3
320 passed in 17.94s

uv run ruff check .
All checks passed!

uv run mypy
Success: no issues found in 149 source files

make check
All checks passed!
Success: no issues found in 149 source files
1492 passed in 41.11s
```

回归矩阵确认 7 keys × 5 surfaces × U+0300/U+0301/CGJ/VS/zero-width 仍全部
识别为 truth；fullwidth disguise 仍识别。`unexpected rows`、普通预组合重音文本和
合法 `expected_evidence` 仍为 false/允许。候选 SQL 的拒绝继续使用精确固定错误
`candidate SQL cannot contain evaluation truth sentinels`，不包含被拒 operand。

## Oracle 19、hash 与数据

使用 Makefile 固定的本地 `analytics_readonly` 连接，在自动清理的临时同仓目录复算：

```text
oracle_recompute_files=19 bytes_match=True second_freeze_rejected=True staging_clean=True
```

- Week 2 frozen：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- Week 3 overall：`c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577`
- Week 3 known：`01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88`
- Week 3 heldout：`a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5`

`git diff --quiet -- evals/datasets/week3 data` 与 `git diff --check` 均通过；协议
hash、全部 frozen bytes 和数据保持不变。

## 结论

本轮 finding 已关闭且没有边界冲突。matcher 现在依据字符来源区分显式攻击分隔符
与 alnum 规范化展开产生的 hard barrier，同时保持兼容折叠、truth 外围 alnum 边界
和固定脱敏错误契约。
