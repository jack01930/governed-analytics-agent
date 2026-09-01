# Task 4 实施报告：异常注入与真值 Manifest

## RED / GREEN

- RED：新增 `test_anomalies.py` 后运行 `uv run pytest tests/unit/data_generation/test_anomalies.py -v`，因 `governed_analytics.data_generation.anomalies` 不存在而在收集阶段失败。
- GREEN：实现后 Task 4 合同测试 6 项通过；全部 data-generation 单测 33 项通过；仓库 `make check` 57 项通过。

## 实现与计数语义

- 新增 `anomalies.py`，提供冻结的 `ExpectedSignal`、`AnomalyRecord`、`AnomalyManifest`、结果容器和 `inject_anomalies`。`dataset_id` 直接采用 `dataset_id_for_config(config)`；`by_id` 对未知 ID 抛出含 ID 的 `KeyError`。
- 固定顺序依次执行：华南转化下降、SKU 缺货、CAT-018 退款激增、库存延迟、明细重复、订单金额不一致、地区为空、退款超过支付。
- 所有选择均先过滤时间/范围再按目标 identity 升序；不使用 RNG。scale→精确数量计划为 tiny `20/10/10/3` 和 full `2000/1000/1000/300`。
- `mutated_rows` 只计主根因实体：翻转 session、删除 item、追加成功 refund、删除 snapshot、复制 item、直接改 order、直接改 refund。订单/支付/退款 FK 的连带校正记录在 `mutation`，不累计。
- 删除/追加后重新连续化 item、refund、inventory identity；以 old→new 映射重连 nullable `refunds.order_item_id`，并保持 `Int64`。
- schema 采用 `json.dumps(model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\\n"` 的 canonical pretty JSON，合同测试比较已提交字节与运行时生成字节。

## 验证与耗时

- `uv run pytest tests/unit/data_generation/test_anomalies.py -v`：6 passed。
- `uv run pytest tests/unit/data_generation -q`：33 passed。
- `uv run ruff check ...`、`uv run mypy ...`：通过。
- `git diff --check`、`make check`：通过，57 passed。
- 本任务未生成 full facts；full 的配额由轻量 helper 与既有 full-anchor 测试锁定，符合任务范围。

## 提交

- 实现提交：`d91efe7 feat: 注入可验证业务与质量异常`。

## 风险

- `dataset_id` 是配置和生成契约版本派生的预加载身份；后续改变生成行为必须同步提升 `GENERATOR_CONTRACT_VERSION`。
- Task 5 写入器必须保留 DataFrame 的当前 identity/FK 关系，并继续排除 PostgreSQL 的 generated identity 列。
