# 第 1C 周：黄金问题与 Text-to-SQL 基线实现计划

> **供 Agent 执行者使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务实施本计划。各步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 构建 20 个由 Oracle 支撑的业务问题，以及安全、可度量的直接 Text-to-SQL 基线，用于建立 Agent 实现前的准确率、失败、延迟与成本参照。

**架构：** 黄金用例与 Oracle SQL 是版本化数据契约。与模型无关的 `SqlGenerator` Protocol 隔离 fixture 与 live 适配器。生成的 SQL 先通过窄范围基线守卫，再使用 PostgreSQL 只读凭据，在只读且有时间上限的事务中执行；评分器比较规范化结果集，并生成 JSON/Markdown 报告。

**技术栈：** Python 3.12、Pydantic 2、SQLGlot、异步 SQLAlchemy、OpenAI Python SDK 3.6、PyYAML、Pytest。

**规格依据：** `GOVERNED_ANALYTICS_AGENT_PLAN.md` 第 20–21、35–37 节，以及 `docs/superpowers/plans/2026-09-01-week-1-data-baseline.md`。

## 全局约束

- 基线只进行一次模型请求，随后执行一次生成 SQL。它不包含规划循环、工具、重试、LangGraph 或隐藏修复步骤。
- 测试与 CI 使用 fixture 模式，不发起外部请求。
- live 模式必须提供 `MODEL_API_KEY`，并同时使用 CLI 标志 `--mode live --live`。
- 探索阶段使用模型别名 `qwen3.7-plus`；记录 provider 返回的 resolved model。
- 对配置的 OpenAI-compatible Base URL 调用 `AsyncOpenAI.chat.completions.create`。不得假设 provider 支持 OpenAI Responses 专属能力。
- 要求输出只包含 `sql` 与 `assumptions` 的 JSON 对象；SDK 返回后使用 Pydantic 解析。
- 不得请求或存储隐藏思维链。`assumptions` 只包含简短业务假设。
- 生成 SQL 以 `analytics_readonly` 身份运行，位于只读事务中，语句超时为 10 秒，默认结果上限为 500 行。
- 不得删除困难用例或覆盖历史报告。每次运行使用稳定的带时间戳目录，并记录全部失败。
- 价格元数据与代码分别版本化，并记录来源/生效日期。

---

## 20 个黄金用例

| ID | 问题 | Oracle 输出 | 比较方式 |
|---|---|---|---|
| `G001` | 2026-06-08 至 2026-06-14 的 GMV 是多少？ | 单行：`gmv` | scalar |
| `G002` | 该周 GMV 相比前一周变化多少？ | `current_gmv`, `previous_gmv`, `change_rate` | table |
| `G003` | 哪些地区对该周 GMV 下滑贡献最大？ | 前 5 个 `region`、`gmv_loss` | top_k |
| `G004` | 哪些商品对该周 GMV 下滑贡献最大？ | 前 5 个 `sku`、`gmv_loss` | top_k |
| `G005` | 各用户分群对该周 GMV 变化贡献如何？ | `segment`, previous/current/delta | table |
| `G006` | 该周 GMV 下滑的主要原因是什么？ | 华南转化变化 + 两个缺货 SKU 的变化 | table |
| `G007` | 2026 年 6 月支付 GMV 是多少？ | 单行：`paid_gmv` | scalar |
| `G008` | 2026 年 5 月净收入是多少？ | 单行：`net_revenue` | scalar |
| `G009` | 2026-05-04 当周退款率最高的品类有哪些？ | 前 5 个 category/refund_rate | top_k |
| `G010` | 该退款异常周最常见的退款原因是什么？ | reason/refund_count/refund_amount | top_k |
| `G011` | 2026-06-08 当周各渠道转化率是多少？ | channel/conversion_rate | table |
| `G012` | 2026-06-08 当周哪些商品发生缺货？ | sku/stockout_days | table |
| `G013` | 2026 年 6 月活跃客户数是多少？ | 单行：`active_customers` | scalar |
| `G014` | 2026 年 6 月新增客户数是多少？ | 单行：`new_customers` | scalar |
| `G015` | 截至 2026-06-30 的复购率是多少？ | 单行：`repeat_purchase_rate` | scalar |
| `G016` | 2026 年第二季度 ROI 最高的营销活动有哪些？ | 前 5 个 campaign/roi | top_k |
| `G017` | 2026-06-15 库存数据是否按时更新？ | 单行：`is_stale` | boolean |
| `G018` | 2026-04-10 有多少条重复订单明细？ | 单行：`duplicate_rows` | scalar |
| `G019` | 2026-03-17 有多少订单金额与明细不一致？ | 单行：`mismatched_orders` | scalar |
| `G020` | 2026-05-20 有多少订单退款超过成功支付？ | 单行：`over_refunded_orders` | scalar |

全部日期区间使用 UTC 半开边界。例如，“2026-06-08 当周”表示 `[2026-06-08T00:00:00Z, 2026-06-15T00:00:00Z)`。

## Oracle SQL 契约

在 `evals/datasets/golden/sql/G001.sql` 至 `G020.sql` 中为每个用例创建一个文件。SQL 使用以下固定模式：

- `G001`：连接目标区间内的有效订单，汇总 `order_items.net_amount`。
- `G002`：对上一窗口与当前窗口做条件聚合；`change_rate = (current - previous) / nullif(previous, 0)`。
- `G003`：上一/当前地区 CTE、full outer join，`gmv_loss = previous - current`，按正损失降序。
- `G004`：按 `products.sku` 使用相同模式。
- `G005`：按 `customers.segment` 使用相同模式，返回全部三个分群。
- `G006`：恰好返回三行证据，`cause_type` 键为 `south_conversion`、`SKU-000001`、`SKU-000002`；每行包含 previous/current/delta。
- `G007`：汇总 `paid_at` 位于 6 月的成功 `payments.amount`。
- `G008`：5 月下单订单的成功支付总额减去成功退款总额。
- `G009`：异常周内按品类分组的订单/订单项，退款金额除以成功支付金额。
- `G010`：异常周成功退款按原因分组，并按退款金额降序。
- `G011`：会话与已转化会话在内部按渠道分组；Oracle 只输出 `channel` 和 `conversion_rate`，比率使用 `nullif(count(*), 0)`。
- `G012`：按 SKU 统计目标周内 `available_qty = 0` 的不同快照日期数。
- `G013`：统计拥有 6 月有效订单的去重客户数。
- `G014`：统计 6 月注册客户数。
- `G015`：截至 2026-07-01 至少有两个有效订单的客户数，除以至少有一个有效订单的客户数。
- `G016`：对与第二季度重叠的营销活动计算（归因收入减花费）/花费，并降序排列。
- `G017`：选择 2026-06-15 最新的库存 `pipeline_runs` 行，根据状态或早于 `2026-06-15T23:59:59Z` 的 watermark 返回一个布尔值 `is_stale`。
- `G018`：对 2026-04-10 订单按 `order_items.source_line_id` 分组；对数量大于一的组汇总 `count(*) - 1`。
- `G019`：对 2026-03-17 的订单，比较每个订单的 `payable_amount` 与订单项 `net_amount + shipping_amount` 合计；容差人民币 0.01 元。
- `G020`：对 2026-05-20 的退款，按订单比较成功退款总额与成功支付总额。

每个 Oracle SQL 文件都以包含用例 ID 与指标版本的注释开头，只包含一条只读语句，使用显式别名，并在多行输出时以确定性的 `order by` 结尾。

## 任务 1：黄金用例契约与注册表

**文件：**
- 新建：`src/governed_analytics/evals/__init__.py`
- 新建：`src/governed_analytics/evals/models.py`
- 新建：`src/governed_analytics/evals/golden.py`
- 新建：`evals/datasets/golden/cases.yaml`
- 新建：`tests/unit/evals/test_golden_registry.py`

**接口：**
- 输入：20 用例表与 Oracle SQL 契约。
- 输出：`GoldenCase`、`BaselineCaseResult`、`load_golden_cases(path) -> tuple[GoldenCase, ...]`。

- [ ] **步骤 1：编写失败的注册表测试**

创建 `tests/unit/evals/test_golden_registry.py`：

```python
from governed_analytics.evals.golden import load_golden_cases


def test_week_one_registry_has_twenty_unique_ordered_cases() -> None:
    cases = load_golden_cases("evals/datasets/golden/cases.yaml")

    assert len(cases) == 20
    assert [case.case_id for case in cases] == [f"G{i:03d}" for i in range(1, 21)]
    assert len({case.question for case in cases}) == 20
    assert all(case.oracle_sql_path.is_file() for case in cases)
```

- [ ] **步骤 2：运行测试并确认评测模块缺失**

运行：

```bash
uv run pytest tests/unit/evals/test_golden_registry.py -v
```

预期：导入 `governed_analytics.evals` 失败。

- [ ] **步骤 3：实现不可变契约**

实现主计划中精确的 `GoldenCase` 与 `BaselineCaseResult`。添加：

```python
class GeneratedSql(BaseModel):
    sql: str
    assumptions: tuple[str, ...] = ()
    provider_model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


class QueryResult(BaseModel):
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


class BaselineRunReport(BaseModel):
    run_id: str
    mode: Literal["fixture", "live"]
    dataset_id: str
    model: str
    result_accuracy: Decimal = Field(ge=0, le=1)
    valid_sql_rate: Decimal = Field(ge=0, le=1)
    execution_success_rate: Decimal = Field(ge=0, le=1)
    total_cost_cny: Decimal = Field(ge=0)
    cases: tuple[BaselineCaseResult, ...]
```

- [ ] **步骤 4：创建 YAML 注册表与加载器**

`cases.yaml` 包含本计划全部 20 个 ID/问题/类别/路径/比较模式。使用以下 `key_columns`：

- `G003`: `region`;
- `G004`, `G012`: `sku`;
- `G005`: `segment`;
- `G006`: `cause_type`;
- `G009`: `category_code`;
- `G010`: `reason`;
- `G011`: `channel`;
- `G016`: `campaign_code`.

使用 Oracle 输出中命名的数值列。`load_golden_cases` 相对仓库根目录解析 SQL 路径，拒绝重复 ID，并验证 ID 匹配 `G[0-9]{3}`。

- [ ] **步骤 5：添加全部 20 个 Oracle SQL 文件并通过注册表测试**

编写 Oracle SQL 契约所述的精确查询。加载时使用 `sqlglot.parse_one(sql, read="postgres")` 解析每条查询。

运行：

```bash
uv run pytest tests/unit/evals/test_golden_registry.py -v
```

预期：一个包含 20 个有序用例的测试通过。

- [ ] **步骤 6：提交黄金契约**

```bash
git add src/governed_analytics/evals evals/datasets/golden tests/unit/evals
git commit -m "feat: 定义二十条黄金业务问题"
```

## 任务 2：物化版本化 Oracle 结果

**文件：**
- 新建：`src/governed_analytics/evals/oracle.py`
- 新建：`evals/datasets/golden/expected/.gitkeep`
- 新建：`tests/integration/evals/test_oracles.py`

**接口：**
- 输入：已加载的 tiny 数据集与 20 个 Oracle SQL 文件。
- 输出：`materialize_oracles(cases, output_dir) -> dict[str, QueryResult]` 及版本化预期 JSON 文件。

- [ ] **步骤 1：编写 Oracle 执行测试**

创建 `tests/integration/evals/test_oracles.py`：

```python
from pathlib import Path

import pytest

from governed_analytics.evals.golden import load_golden_cases
from governed_analytics.evals.oracle import materialize_oracles


@pytest.mark.integration
def test_all_oracles_execute_and_materialize(tmp_path: Path) -> None:
    cases = load_golden_cases("evals/datasets/golden/cases.yaml")
    results = materialize_oracles(cases, tmp_path)

    assert set(results) == {case.case_id for case in cases}
    assert all((tmp_path / f"{case.case_id}.json").is_file() for case in cases)
    assert results["G018"].rows[0][0] == 20
    assert results["G019"].rows[0][0] == 10
    assert results["G020"].rows[0][0] == 3
```

- [ ] **步骤 2：运行测试并确认 Oracle 执行器缺失**

运行：

```bash
uv run pytest tests/integration/evals/test_oracles.py -v
```

预期：导入 `oracle` 失败。

- [ ] **步骤 3：实现只读 Oracle 执行**

使用只读专用 `DatabaseSettings.database_url` 与异步 engine。每条 Oracle 语句都必须在显式事务中运行，并先在同一连接上执行 `SET TRANSACTION READ ONLY`、`SET LOCAL statement_timeout = '10s'` 和 `SET LOCAL search_path = public, pg_catalog`。通过 Pydantic 序列化将 `Decimal` 与时间戳转换为 JSON 字符串。对象键排序，JSON 文件以一个换行结尾。真实 Oracle 集成测试必须在该事务中查询 `current_setting`，并断言 `transaction_read_only = 'on'`、`statement_timeout = '10s'` 和 `search_path = 'public, pg_catalog'`。

提供同步命令包装器，仅在 CLI 边界使用 `asyncio.run`；内部函数保持异步。

- [ ] **步骤 4：物化并提交 tiny 预期结果**

运行：

```bash
uv run python -m governed_analytics.evals.oracle \
  --cases evals/datasets/golden/cases.yaml \
  --output evals/datasets/golden/expected
uv run pytest tests/integration/evals/test_oracles.py -v
```

预期：创建 20 个 JSON 文件，且固定种子异常的精确数量通过。

```bash
git add src/governed_analytics/evals/oracle.py evals/datasets/golden/expected tests/integration/evals/test_oracles.py
git commit -m "test: 固化黄金查询预期结果"
```

## 任务 3：模型 Protocol、Prompt 与 Fixture 适配器

**文件：**
- 新建：`src/governed_analytics/models/__init__.py`
- 新建：`src/governed_analytics/models/protocols.py`
- 新建：`src/governed_analytics/models/prompts.py`
- 新建：`src/governed_analytics/models/fixtures.py`
- 新建：`evals/fixtures/baseline_sql.json`
- 新建：`tests/unit/models/test_fixture_generator.py`

**接口：**
- 输入：问题、Schema 摘要、指标上下文及 fixture SQL。
- 输出：`SqlGenerationRequest`、`SqlGenerator`，以及 `FixtureSqlGenerator.generate(request) -> GeneratedSql`。

- [ ] **步骤 1：编写无网络 fixture 适配器测试**

创建 `tests/unit/models/test_fixture_generator.py`：

```python
import pytest

from governed_analytics.models.fixtures import FixtureSqlGenerator
from governed_analytics.models.protocols import SqlGenerationRequest


@pytest.mark.asyncio
async def test_fixture_generator_returns_case_sql_without_network() -> None:
    generator = FixtureSqlGenerator.from_path("evals/fixtures/baseline_sql.json")
    request = SqlGenerationRequest(
        case_id="G001",
        question="2026-06-08 至 2026-06-14 的 GMV 是多少？",
        schema_context="orders(order_id, status, ordered_at); order_items(order_id, net_amount)",
        metric_context="gmv = sum(order_items.net_amount) for valid orders",
    )

    result = await generator.generate(request)

    assert result.sql.lower().startswith("select") or result.sql.lower().startswith("with")
    assert result.provider_model == "fixture-oracle"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
```

- [ ] **步骤 2：定义与模型无关的接口**

创建 `protocols.py`：

```python
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from governed_analytics.evals.models import GeneratedSql


class SqlGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    question: str
    schema_context: str
    metric_context: str


class SqlGenerator(Protocol):
    async def generate(self, request: SqlGenerationRequest) -> GeneratedSql: ...
```

- [ ] **步骤 3：冻结基线 Prompt**

`prompts.py` 导出满足以下要求的 `BASELINE_SYSTEM_PROMPT_V1`：

```text
You generate exactly one PostgreSQL read-only query for the supplied ecommerce question.
Use only tables and columns in SCHEMA CONTEXT and metric rules in METRIC CONTEXT.
Use half-open UTC time intervals. Do not invent columns or metrics.
Return one JSON object with keys "sql" and "assumptions".
The SQL must be one SELECT or WITH query. Do not include Markdown fences.
```

`build_baseline_user_prompt(request)` 以稳定顺序标记问题、Schema 上下文与指标上下文。

- [ ] **步骤 4：添加 20 条 fixture 响应**

`evals/fixtures/baseline_sql.json` 将每个用例 ID 映射到对应 Oracle SQL 文本。该 fixture 用于验证编排、安全、执行、评分与报告；其分数绝不能表述为 live 模型质量。

- [ ] **步骤 5：运行测试并提交模型边界**

运行：

```bash
uv run pytest tests/unit/models/test_fixture_generator.py -v
uv run mypy src/governed_analytics/models
```

预期：在无网络访问的情况下，测试与类型检查通过。

```bash
git add src/governed_analytics/models evals/fixtures tests/unit/models
git commit -m "feat: 隔离基线模型接口与离线夹具"
```

## 任务 4：窄范围基线 SQL 守卫与执行器

**文件：**
- 新建：`src/governed_analytics/evals/sql_guard.py`
- 新建：`src/governed_analytics/evals/executor.py`
- 新建：`tests/unit/evals/test_sql_guard.py`
- 新建：`tests/integration/evals/test_readonly_executor.py`

**接口：**
- 输入：模型生成的 SQL。
- 输出：`validate_baseline_sql(sql) -> str` 与 `execute_readonly_sql(sql) -> QueryResult`。

- [ ] **步骤 1：编写守卫允许/拒绝测试**

创建 `tests/unit/evals/test_sql_guard.py`：

```python
import pytest

from governed_analytics.evals.sql_guard import SqlRejected, validate_baseline_sql


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from orders",
        "with totals as (select sum(net_amount) value from order_items) select value from totals",
    ],
)
def test_guard_allows_single_read_query_and_adds_limit(sql: str) -> None:
    validated = validate_baseline_sql(sql)
    assert "LIMIT 500" in validated.upper()


@pytest.mark.parametrize(
    "sql",
    [
        "delete from orders",
        "select 1; select 2",
        "select pg_sleep(30)",
        "copy orders to '/tmp/orders.csv'",
        "select pg_read_file('/etc/passwd')",
    ],
)
def test_guard_rejects_unsafe_sql(sql: str) -> None:
    with pytest.raises(SqlRejected):
        validate_baseline_sql(sql)
```

- [ ] **步骤 2：实现 AST 验证**

使用 `sqlglot.parse(sql, read="postgres")` 解析；要求恰好一个表达式，且必须为 `exp.Query`。拒绝以下类的任何后代节点：

```python
DENIED_NODES = (
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.Transaction,
    exp.Update,
)
```

拒绝以下函数名：

```python
DENIED_FUNCTIONS = frozenset(
    {"dblink", "lo_export", "lo_import", "pg_ls_dir", "pg_read_file", "pg_sleep", "set_config"}
)
```

使用 SQLGlot AST 添加外层查询上限或将其缩减为 500，再序列化为 PostgreSQL 方言。第 2 周将用完整策略引擎替换该窄范围守卫。

- [ ] **步骤 3：实现只读执行器**

使用：

```python
async with engine.connect() as connection:
    async with connection.begin():
        await connection.execute(text("set transaction read only"))
        await connection.execute(text("set local statement_timeout = '10s'"))
        await connection.execute(text("set local search_path = public, pg_catalog"))
        result = await connection.execute(text(validated_sql))
        rows = tuple(tuple(row) for row in result.fetchall())
        return QueryResult(columns=tuple(result.keys()), rows=rows)
```

engine 只能由只读专用 `DatabaseSettings.database_url` 创建。不得将事务状态添加到通用 engine 工厂。真实 PostgreSQL 执行器集成测试必须通过该执行器事务查询 `current_setting`，并断言 `transaction_read_only = 'on'`、`statement_timeout = '10s'` 及 `search_path = 'public, pg_catalog'`。

- [ ] **步骤 4：证明数据库权限是守卫的后备防线**

创建集成测试，将 `validate_baseline_sql` monkeypatch 为返回 `insert into categories ...`，并断言 PostgreSQL 抛出 `InsufficientPrivilege`。这证明即使绕过守卫也无法写入。

- [ ] **步骤 5：运行测试并提交基线执行安全实现**

运行：

```bash
uv run pytest tests/unit/evals/test_sql_guard.py -v
uv run pytest tests/integration/evals/test_readonly_executor.py -v
```

预期：全部允许/拒绝用例通过，只读数据库拒绝 DML。

```bash
git add src/governed_analytics/evals/sql_guard.py src/governed_analytics/evals/executor.py tests/unit/evals/test_sql_guard.py tests/integration/evals/test_readonly_executor.py
git commit -m "feat: 安全执行只读基线查询"
```

## 任务 5：结果评分与报告

**文件：**
- 新建：`src/governed_analytics/evals/scorers.py`
- 新建：`src/governed_analytics/evals/reporting.py`
- 新建：`tests/unit/evals/test_scorers.py`
- 新建：`tests/unit/evals/test_reporting.py`

**接口：**
- 输入：`GoldenCase`、预期 `QueryResult`、实际 `QueryResult` 与 `BaselineCaseResult`。
- 输出：`score_result(...) -> Decimal`、聚合 `BaselineRunReport`、JSON 与 Markdown 报告。

- [ ] **步骤 1：编写精确评分器测试**

覆盖以下用例：

```python
def test_scalar_uses_absolute_cent_tolerance() -> None:
    assert score_scalar(Decimal("100.00"), Decimal("100.009"), Decimal("0.01"), Decimal("0")) == Decimal("1")


def test_table_is_order_independent_but_key_exact() -> None:
    expected = (("广州", Decimal("10.00")), ("深圳", Decimal("20.00")))
    actual = (("深圳", Decimal("20.00")), ("广州", Decimal("10.00")))
    assert score_keyed_table(expected, actual, key_indexes=(0,), numeric_indexes=(1,)) == Decimal("1")


def test_top_k_returns_overlap_fraction() -> None:
    assert score_top_k(("A", "B", "C"), ("A", "C", "D")) == Decimal("0.666667")
```

- [ ] **步骤 2：实现比较语义**

- scalar：一个数值，位于绝对或相对容差内；
- table：键集合精确相同、非数值单元格精确相同、每个数值单元格按容差比较，忽略行顺序；
- top_k：键重合数除以预期 K，四舍五入到六位小数；
- boolean：规范化布尔值精确匹配。

仅当分数等于 1 时，用例状态才为 `passed`。有效但部分正确的 `top_k` 结果仍标记为 `wrong_answer`，并保留部分分数。

- [ ] **步骤 3：实现聚合指标**

计算：

```python
result_accuracy = sum(case.score for case in cases) / Decimal(len(cases))
valid_sql_rate = valid_sql_cases / Decimal(len(cases))
execution_success_rate = executed_cases / Decimal(len(cases))
total_cost_cny = sum(case.estimated_cost_cny for case in cases)
```

展示的比率四舍五入到四位小数，成本四舍五入到六位小数；JSON 中保留未舍入的逐用例值。

- [ ] **步骤 4：生成不可变 JSON 与 Markdown 报告**

目录格式：

```text
artifacts/evals/baseline/<mode>/<YYYYMMDDTHHMMSSZ>-<run_id>/
├── report.json
├── report.md
└── cases/
    └── G001.json
```

Markdown 包含数据集 ID、Prompt 版本、请求/实际模型、结果准确率、有效 SQL 率、执行率、总/平均成本、P50/P95 延迟、失败数量及 20 行用例表。fixture 报告必须明确标注“评测链路验证，不代表模型质量”。

- [ ] **步骤 5：运行测试并提交评分/报告实现**

运行：

```bash
uv run pytest tests/unit/evals/test_scorers.py tests/unit/evals/test_reporting.py -v
```

预期：评分器边界用例与快照规范化报告测试通过。

```bash
git add src/governed_analytics/evals/scorers.py src/governed_analytics/evals/reporting.py tests/unit/evals/test_scorers.py tests/unit/evals/test_reporting.py
git commit -m "feat: 评估基线结果并生成报告"
```

## 任务 6：OpenAI-compatible Live 适配器与成本核算

**文件：**
- 新建：`src/governed_analytics/models/openai_compatible.py`
- 新建：`src/governed_analytics/evals/pricing.py`
- 新建：`data/pricing/qwen3.7-plus-2026-09-01.yaml`
- 修改：`src/governed_analytics/config.py`
- 新建：`tests/unit/models/test_openai_compatible.py`
- 新建：`tests/unit/evals/test_pricing.py`

**接口：**
- 输入：`AsyncOpenAI`、`SqlGenerationRequest`、模型设置及价格 YAML。
- 输出：`OpenAICompatibleSqlGenerator` 与 `estimate_cost_cny(...) -> Decimal`。

- [ ] **步骤 1：编写伪 SDK 响应测试**

使用具有类型的伪对象，其 `chat.completions.create` 异步方法记录参数并返回以下内容：

```json
{"sql":"select count(*) as order_count from orders","assumptions":["有效订单口径由指标目录提供"]}
```

断言适配器发送 `model`、稳定消息、`temperature=0`、`response_format={"type": "json_object"}` 和 `max_completion_tokens=1200`；同时断言返回的 usage 与 provider model 得以保留。

- [ ] **步骤 2：通过 client 注入实现适配器**

向 `src/governed_analytics/config.py` 添加模型设置：

```python
from pydantic import SecretStr


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_api_key: SecretStr | None = None
    model_name: str = "qwen3.7-plus"
    eval_model_name: str = "qwen3.7-plus-2026-05-26"
```

CLI 仅在构造 `AsyncOpenAI` 时解包 `model_api_key`；日志与报告绝不序列化 `SecretStr`。

构造函数：

```python
class OpenAICompatibleSqlGenerator:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model
```

调用已安装 SDK 的以下接口：

```python
response = await self._client.chat.completions.create(
    model=self._model,
    messages=[
        {"role": "system", "content": BASELINE_SYSTEM_PROMPT_V1},
        {"role": "user", "content": build_baseline_user_prompt(request)},
    ],
    temperature=0,
    response_format={"type": "json_object"},
    max_completion_tokens=1200,
)
```

使用私有 Pydantic 响应模型验证 `response.choices[0].message.content`。将内容缺失、JSON 无效或 usage 缺失分类为适配器错误；仅 fixture 模式允许 usage 为零。

- [ ] **步骤 3：版本化 Qwen 价格元数据**

创建：

```yaml
provider: aliyun_model_studio
model: qwen3.7-plus
effective_date: 2026-09-01
currency: CNY
unit_tokens: 1000000
input_price: "2.00"
output_price: "8.00"
source: https://help.aliyun.com/zh/model-studio/model-pricing
```

成本公式只使用 `Decimal`：

```python
cost = (
    Decimal(input_tokens) * input_price / Decimal(unit_tokens)
    + Decimal(output_tokens) * output_price / Decimal(unit_tokens)
)
```

- [ ] **步骤 4：运行测试并提交 live 适配器**

运行：

```bash
uv run pytest tests/unit/models/test_openai_compatible.py tests/unit/evals/test_pricing.py -v
```

预期：在不发起网络请求的情况下，伪 client 参数、响应解析及精确 Decimal 成本测试通过。

```bash
git add src/governed_analytics/config.py src/governed_analytics/models/openai_compatible.py src/governed_analytics/evals/pricing.py data/pricing tests/unit/models/test_openai_compatible.py tests/unit/evals/test_pricing.py
git commit -m "feat: 接入兼容模型并记录调用成本"
```

## 任务 7：基线执行器、CLI、CI 与文档

**文件：**
- 新建：`src/governed_analytics/evals/runner.py`
- 新建：`src/governed_analytics/evals/cli.py`
- 新建：`tests/integration/evals/test_baseline_runner.py`
- 修改：`pyproject.toml`
- 修改：`Makefile`
- 修改：`.github/workflows/ci.yml`
- 新建：`docs/evals.md`
- 修改：`README.md`

**接口：**
- 输入：用例、Schema/指标上下文、fixture 或 live 生成器、守卫、执行器、Oracle 结果、评分器与价格。
- 输出：`governed-eval baseline` 及完整基线报告。

- [ ] **步骤 1：添加控制台入口**

添加：

```toml
[project.scripts]
governed-data = "governed_analytics.data_generation.cli:main"
governed-eval = "governed_analytics.evals.cli:main"
```

- [ ] **步骤 2：编写 fixture 执行器集成测试**

创建 `tests/integration/evals/test_baseline_runner.py`：

```python
from pathlib import Path

import pytest

from governed_analytics.evals.runner import run_baseline


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fixture_baseline_executes_all_cases_without_cost(tmp_path: Path) -> None:
    report = await run_baseline(mode="fixture", output_root=tmp_path)

    assert len(report.cases) == 20
    assert report.result_accuracy == 1
    assert report.valid_sql_rate == 1
    assert report.execution_success_rate == 1
    assert report.total_cost_cny == 0
    assert all(case.status == "passed" for case in report.cases)
```

- [ ] **步骤 3：实现单次通过的用例执行**

对每个用例严格执行一次：

1. 创建 `SqlGenerationRequest`；
2. 调用一次 `generator.generate`；
3. 验证一次 SQL；
4. 执行一次 SQL；
5. 与已提交的 Oracle 结果比较；
6. 记录结果、延迟、usage、成本及任何失败类别。

只在用例边界捕获并分类异常。失败后继续下一个用例。第 1 周不得重试或修复 SQL。

- [ ] **步骤 4：在 CLI 中强制执行 live 授权**

CLI 接口：

```text
governed-eval baseline --dataset tiny --mode fixture
governed-eval baseline --dataset tiny --mode live --live
```

若模式为 live 但缺少 `--live`，以状态码 2 退出并提示 `Live model calls require --live`。若 Key 缺失，以状态码 2 退出并提示 `MODEL_API_KEY is not configured`。第 1 周拒绝对 full 数据集运行基线。

- [ ] **步骤 5：添加仅限 fixture 的 Make 与 CI 命令**

```make
.PHONY: eval-fixture

eval-fixture:
	@uv run governed-eval baseline --dataset tiny --mode fixture
```

在 CI 的 tiny 数据/指标检查之后添加：

```yaml
      - run: uv run governed-eval baseline --dataset tiny --mode fixture
```

不得向 CI 添加 `MODEL_API_KEY`，也不得运行 live 模式。

- [ ] **步骤 6：记录报告解读与 OpenAI-compatible 边界**

`docs/evals.md` 记录用例 Schema、Oracle 物化、评分器语义、失败类别、fixture/live 区别、价格快照、显式 live 授权，以及“live 结果才是基线，fixture 的 100% 不是模型成绩”这一事实。

引用适配器使用的官方 Chat Completions 接口：`https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions`。

- [ ] **步骤 7：运行完整 Plan C 门禁**

运行：

```bash
make db-up
make migrate
make data-tiny
make metrics-check
make eval-fixture
make check
uv run pytest tests/integration/evals -v
git diff --check
```

预期：20 个 fixture 用例通过，不发起外部调用，全部质量检查通过，报告生成在被忽略的 artifacts 下。

- [ ] **步骤 8：提交 Plan C 工作流**

```bash
git add pyproject.toml uv.lock src/governed_analytics/evals src/governed_analytics/models Makefile .github/workflows/ci.yml docs/evals.md README.md
git commit -m "feat: 建立可复现 Text-to-SQL 基线"
```

## Plan C 完成门禁

- [ ] 20 个用例定义、20 个 Oracle SQL 文件及 20 个预期 JSON 文件均已版本化。
- [ ] 在 tiny 数据上，精确异常用例 G018–G020 分别返回固定种子数量 `20`、`10`、`3`。
- [ ] fixture 模式执行全部 20 个用例，Token 为零、人民币成本为零、无网络访问。
- [ ] 不安全/多语句 SQL 在执行前被拒；PostgreSQL 权限独立拒绝 DML。
- [ ] 报告包含准确率、有效 SQL 率、执行率、P50/P95 延迟、Token、成本及全部失败。
- [ ] live 调用需要显式双重授权，且绝不在 CI 中运行。
- [ ] 首次获授权的 live 报告即使准确率很差也要保留。
- [ ] 不存在 LangGraph 或修复循环，从而保留该基线比较的价值。
