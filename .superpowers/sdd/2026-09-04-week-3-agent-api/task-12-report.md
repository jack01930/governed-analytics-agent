# Task 12 交付报告：Week 3 独立评测协议

## 结果

- 建立独立 `governed_analytics.evals.week3` wire models、严格 registry/script/truth loader、overall/cohort hash 与一次性 tiny Oracle freezer。
- 固定 30 个 known case、10 个 heldout case、30 个 known fixture script、24 份独立 candidate SQL、19 份 Oracle SQL 和 19 份 frozen expected JSON。
- Week 2 的 70 个 case ID 和 suite manifest 保持 byte-frozen；未固定会随 Agent 源码变化的 implementation hash。
- 未修改 controller ledger `progress.md`，未修改 Task 11 `tests/integration/api/test_api_sse.py`，未调用网络、live 模型或任何 API client。

## RED

1. `uv run pytest tests/unit/evals/test_week2_frozen_protocol.py -v`
   - 首次测试编写时 archive manifest 定位字段写错，得到 `1 passed, 1 failed`；修正为按 `label=week2-live-v1` 查找后 `2 passed`。
   - Week 2 suite hash 为 `ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`。
2. `uv run pytest tests/unit/evals/week3/test_models.py tests/unit/evals/week3/test_suites.py -q`
   - 按预期 collection RED：2 个 `ModuleNotFoundError`，缺少 `governed_analytics.evals.week3`。

## GREEN 与验证

- `uv run pytest tests/unit/evals/test_week2_frozen_protocol.py tests/unit/evals/week3 -q`：`28 passed`。
- hash mutation 参数测试覆盖 known registry、heldout registry、scripts registry、candidate SQL、Oracle SQL、expected JSON；每类 byte 变化均改变 overall hash。
- `uv run pytest tests/unit -q`：初次 `1199 passed`；加入最终 cohort mutation 门禁后为 `1200 passed`。
- `uv run ruff check src/governed_analytics/evals/week3 tests/unit/evals/week3 tests/unit/evals/test_week2_frozen_protocol.py`：通过。
- `uv run mypy src/governed_analytics/evals/week3 tests/unit/evals/week3 tests/unit/evals/test_week2_frozen_protocol.py`：通过。
- `make check`：ruff 全仓通过，mypy `149 source files` 通过，unit `1200 passed`。
- `make migrate && make data-tiny`：通过，tiny dataset ID 为 `a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`。
- 首次 `uv run python -m governed_analytics.evals.week3.fixtures --dataset tiny`：生成恰好 19 个 JSON，无 staging 残留。
- 第二次相同 freezer 命令：在连接数据库前以 `FileExistsError` 整批拒绝；unit spy 验证零 DB 触达。
- 当前 tiny DB 上逐条执行 19 个 production-policy validated `driver_sql`：columns/rows/oracle query ID 全部与 frozen JSON 一致。

## Frozen expected 文件

`W3K011.json`、`W3K012.json`、`W3K013.json`、`W3K014.json`、`W3K015.json`、`W3K016.json`、`W3K017.json`、`W3K018.json`、`W3K019.json`、`W3K020.json`、`W3K021.json`、`W3K022.json`、`W3K023.json`、`W3K024.json`、`W3K025.json`、`W3K026-confirm_decline.json`、`W3K026-region_contribution.json`、`W3K026-sku_contribution.json`、`W3K026-segment_contribution.json`。

数值按协议序列化：Decimal 为字符串，计数为 JSON integer，datetime（若出现）规范到 UTC `Z`，bool/null 保持原 JSON 类型。

## 最终 hashes

- Week 2 frozen suite：`ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d`
- Week 3 overall：`e8ba86bce6022e5f65ba5218e27926dbe1d0d8675e0f774e2ac35502fe5cdbb2`
- Week 3 known cohort：`4b282d6d585dcb0cc2b0733f34acc7e9d7cb85ebff8c032ad9290a891684be6a`
- Week 3 heldout cohort：`8f747c9b47feb269d31d6575cccd01944a6e0fab2b4eb394addf33dc972783a3`

## 规格裁决与风险

- W3K029 按真实 graph 的 deterministic terminal mapping 固定为 `budget_exhausted/tool_call_limit`；comparison 已形成证据，case 同时保留 `partial_evidence` 风险标签。第二个 action 会生成，但第 4 次工具额度在 backend 前拒绝。
- `make db-up` 因本机 `127.0.0.1:5432` 已被现有本地 PostgreSQL 容器占用而无法创建本 worktree 的同端口容器；未停止或删除该容器。后续核验其 `analytics_readonly`、3000 orders，并在该本地缓存实例上成功运行 migrate、data-tiny、freezer 和 19 条只读复算。
- freezer 不调用 public loader，因此首次 freeze 不依赖 expected 目录；成功后 public loader 才执行 19 文件严格 reload、Oracle identity 和 inventory 校验。
