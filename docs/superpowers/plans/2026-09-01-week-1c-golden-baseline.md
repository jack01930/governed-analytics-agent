# Week 1C Golden Questions and Text-to-SQL Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build twenty oracle-backed business questions and a safe, measurable direct Text-to-SQL baseline that establishes the pre-Agent accuracy, failure, latency, and cost reference.

**Architecture:** Golden cases and oracle SQL are versioned data contracts. A model-independent `SqlGenerator` Protocol separates fixture and live adapters. Generated SQL passes a narrow baseline guard and executes with PostgreSQL read-only credentials inside a read-only, time-bounded transaction; scorers compare normalized result sets and generate JSON/Markdown reports.

**Tech Stack:** Python 3.12, Pydantic 2, SQLGlot, SQLAlchemy async, OpenAI Python SDK 3.6, PyYAML, Pytest.

**Spec:** `GOVERNED_ANALYTICS_AGENT_PLAN.md` sections 20-21 and 35-37, plus `docs/superpowers/plans/2026-09-01-week-1-data-baseline.md`.

## Global Constraints

- The baseline is one model request followed by one generated SQL query. It has no planning loop, tools, retries, LangGraph, or hidden repair step.
- Tests and CI use fixture mode and make zero external requests.
- Live mode requires `MODEL_API_KEY` plus both CLI flags `--mode live --live`.
- Use model alias `qwen3.7-plus` for exploration; record the resolved model returned by the provider.
- Use `AsyncOpenAI.chat.completions.create` against the configured OpenAI-compatible base URL. Do not assume the provider supports OpenAI Responses-specific features.
- Ask for JSON object output containing only `sql` and `assumptions`; parse it with Pydantic after SDK return.
- Never request or store hidden chain-of-thought. `assumptions` contains short business assumptions only.
- Generated SQL runs as `analytics_readonly`, in a read-only transaction, with 10-second statement timeout and 500-row default result limit.
- Never delete difficult cases or overwrite prior reports. Each run gets a stable timestamped directory and records every failure.
- Price metadata is versioned separately from code and records source/effective date.

---

## Twenty Golden Cases

| ID | Question | Oracle output | Comparison |
|---|---|---|---|
| `G001` | 2026-06-08 至 2026-06-14 的 GMV 是多少？ | one row: `gmv` | scalar |
| `G002` | 该周 GMV 相比前一周变化多少？ | `current_gmv`, `previous_gmv`, `change_rate` | table |
| `G003` | 哪些地区对该周 GMV 下滑贡献最大？ | top 5 `region`, `gmv_loss` | top_k |
| `G004` | 哪些商品对该周 GMV 下滑贡献最大？ | top 5 `sku`, `gmv_loss` | top_k |
| `G005` | 各用户分群对该周 GMV 变化贡献如何？ | `segment`, previous/current/delta | table |
| `G006` | 该周 GMV 下滑的主要原因是什么？ | South conversion delta plus two stockout SKU deltas | table |
| `G007` | 2026 年 6 月支付 GMV 是多少？ | one row: `paid_gmv` | scalar |
| `G008` | 2026 年 5 月净收入是多少？ | one row: `net_revenue` | scalar |
| `G009` | 2026-05-04 当周退款率最高的品类有哪些？ | top 5 category/refund_rate | top_k |
| `G010` | 该退款异常周最常见的退款原因是什么？ | reason/refund_count/refund_amount | top_k |
| `G011` | 2026-06-08 当周各渠道转化率是多少？ | channel/session_count/conversion_rate | table |
| `G012` | 2026-06-08 当周哪些商品发生缺货？ | sku/stockout_days | table |
| `G013` | 2026 年 6 月活跃客户数是多少？ | one row: `active_customers` | scalar |
| `G014` | 2026 年 6 月新增客户数是多少？ | one row: `new_customers` | scalar |
| `G015` | 截至 2026-06-30 的复购率是多少？ | one row: `repeat_purchase_rate` | scalar |
| `G016` | 2026 年第二季度 ROI 最高的营销活动有哪些？ | top 5 campaign/roi | top_k |
| `G017` | 2026-06-15 库存数据是否按时更新？ | one row: `is_stale` | boolean |
| `G018` | 2026-04-10 有多少条重复订单明细？ | one row: `duplicate_rows` | scalar |
| `G019` | 2026-03-17 有多少订单金额与明细不一致？ | one row: `mismatched_orders` | scalar |
| `G020` | 2026-05-20 有多少订单退款超过成功支付？ | one row: `over_refunded_orders` | scalar |

All date intervals use half-open UTC boundaries. For example, “2026-06-08 当周” is `[2026-06-08T00:00:00Z, 2026-06-15T00:00:00Z)`.

## Oracle SQL Contract

Create one file per case under `evals/datasets/golden/sql/G001.sql` through `G020.sql`. SQL uses these fixed patterns:

- `G001`: sum `order_items.net_amount` joined to valid orders in the target interval.
- `G002`: conditional aggregate for previous and current intervals; `change_rate = (current - previous) / nullif(previous, 0)`.
- `G003`: previous/current region CTEs, full outer join, `gmv_loss = previous - current`, descending positive loss.
- `G004`: same pattern by `products.sku`.
- `G005`: same pattern by `customers.segment`, returning all three segments.
- `G006`: return exactly three evidence rows with `cause_type` keys `south_conversion`, `SKU-000001`, and `SKU-000002`; each row has previous/current/delta.
- `G007`: sum succeeded `payments.amount` where `paid_at` is in June.
- `G008`: succeeded payment total minus succeeded refund total for orders placed in May.
- `G009`: refund amount divided by succeeded payment amount for orders/items grouped by category in the anomaly week.
- `G010`: succeeded refunds grouped by reason in the anomaly week, ordered by refund amount descending.
- `G011`: sessions and converted sessions grouped by channel; ratio uses `nullif(count(*), 0)`.
- `G012`: count distinct snapshot dates with `available_qty = 0` per SKU in the target week.
- `G013`: count distinct customers with valid June orders.
- `G014`: count customers registered in June.
- `G015`: customers with at least two valid orders by 2026-07-01 divided by customers with at least one.
- `G016`: attributed revenue minus spend divided by spend for campaigns overlapping Q2, ordered descending.
- `G017`: select the latest inventory `pipeline_runs` row on 2026-06-15 and return one boolean `is_stale` from status or watermark before `2026-06-15T23:59:59Z`.
- `G018`: group `order_items.source_line_id` for orders on 2026-04-10; sum `count(*) - 1` for groups above one.
- `G019`: compare each order's `payable_amount` to summed item `net_amount + shipping_amount` for orders on 2026-03-17; tolerance CNY 0.01.
- `G020`: compare successful refund sum to successful payment sum per order for refunds on 2026-05-20.

Every oracle SQL file starts with a comment containing case ID and metric version, contains one read-only statement, has explicit aliases, and ends with deterministic `order by` for multi-row output.

## Task 1: Golden Case Contracts and Registry

**Files:**
- Create: `src/governed_analytics/evals/__init__.py`
- Create: `src/governed_analytics/evals/models.py`
- Create: `src/governed_analytics/evals/golden.py`
- Create: `evals/datasets/golden/cases.yaml`
- Create: `tests/unit/evals/test_golden_registry.py`

**Interfaces:**
- Consumes: the twenty-case table and Oracle SQL contract.
- Produces: `GoldenCase`, `BaselineCaseResult`, `load_golden_cases(path) -> tuple[GoldenCase, ...]`.

- [ ] **Step 1: Write a failing registry test**

Create `tests/unit/evals/test_golden_registry.py`:

```python
from governed_analytics.evals.golden import load_golden_cases


def test_week_one_registry_has_twenty_unique_ordered_cases() -> None:
    cases = load_golden_cases("evals/datasets/golden/cases.yaml")

    assert len(cases) == 20
    assert [case.case_id for case in cases] == [f"G{i:03d}" for i in range(1, 21)]
    assert len({case.question for case in cases}) == 20
    assert all(case.oracle_sql_path.is_file() for case in cases)
```

- [ ] **Step 2: Run and verify missing eval modules**

Run:

```bash
uv run pytest tests/unit/evals/test_golden_registry.py -v
```

Expected: imports fail for `governed_analytics.evals`.

- [ ] **Step 3: Implement immutable contracts**

Implement the exact `GoldenCase` and `BaselineCaseResult` from the master plan. Add:

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

- [ ] **Step 4: Create the YAML registry and loader**

`cases.yaml` contains all twenty IDs/questions/categories/paths/comparison modes from this plan. Use `key_columns`:

- `G003`: `region`;
- `G004`, `G012`: `sku`;
- `G005`: `segment`;
- `G006`: `cause_type`;
- `G009`: `category_code`;
- `G010`: `reason`;
- `G011`: `channel`;
- `G016`: `campaign_code`.

Use numeric columns named in the Oracle output. `load_golden_cases` resolves SQL paths relative to the repository root, rejects duplicate IDs, and verifies IDs match `G[0-9]{3}`.

- [ ] **Step 5: Add all twenty Oracle SQL files and pass registry tests**

Write the exact queries described in the Oracle SQL Contract. Parse each with `sqlglot.parse_one(sql, read="postgres")` during loading.

Run:

```bash
uv run pytest tests/unit/evals/test_golden_registry.py -v
```

Expected: one test passes with twenty ordered cases.

- [ ] **Step 6: Commit golden contracts**

```bash
git add src/governed_analytics/evals evals/datasets/golden tests/unit/evals
git commit -m "feat: 定义二十条黄金业务问题"
```

## Task 2: Materialize Versioned Oracle Results

**Files:**
- Create: `src/governed_analytics/evals/oracle.py`
- Create: `evals/datasets/golden/expected/.gitkeep`
- Create: `tests/integration/evals/test_oracles.py`

**Interfaces:**
- Consumes: tiny loaded dataset and twenty Oracle SQL files.
- Produces: `materialize_oracles(cases, output_dir) -> dict[str, QueryResult]` and versioned expected JSON files.

- [ ] **Step 1: Write an Oracle execution test**

Create `tests/integration/evals/test_oracles.py`:

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

- [ ] **Step 2: Run and verify missing Oracle executor**

Run:

```bash
uv run pytest tests/integration/evals/test_oracles.py -v
```

Expected: import fails for `oracle`.

- [ ] **Step 3: Implement read-only Oracle execution**

Use the readonly-only `DatabaseSettings.database_url` and an async engine. Every Oracle statement
runs inside an explicit transaction after `SET TRANSACTION READ ONLY`,
`SET LOCAL statement_timeout = '10s'`, and
`SET LOCAL search_path = public, pg_catalog` on the same connection. Convert `Decimal` and
timestamps to JSON strings through Pydantic serialization. Sort object keys and end JSON files with
one newline. The real Oracle integration suite must query `current_setting` in that transaction and
assert `transaction_read_only = 'on'`, `statement_timeout = '10s'`, and
`search_path = 'public, pg_catalog'`.

Expose a synchronous command wrapper that uses `asyncio.run` only at the CLI boundary; internal functions remain async.

- [ ] **Step 4: Materialize and commit tiny expected results**

Run:

```bash
uv run python -m governed_analytics.evals.oracle \
  --cases evals/datasets/golden/cases.yaml \
  --output evals/datasets/golden/expected
uv run pytest tests/integration/evals/test_oracles.py -v
```

Expected: twenty JSON files are created and exact seeded anomaly counts pass.

```bash
git add src/governed_analytics/evals/oracle.py evals/datasets/golden/expected tests/integration/evals/test_oracles.py
git commit -m "test: 固化黄金查询预期结果"
```

## Task 3: Model Protocol, Prompt, and Fixture Adapter

**Files:**
- Create: `src/governed_analytics/models/__init__.py`
- Create: `src/governed_analytics/models/protocols.py`
- Create: `src/governed_analytics/models/prompts.py`
- Create: `src/governed_analytics/models/fixtures.py`
- Create: `evals/fixtures/baseline_sql.json`
- Create: `tests/unit/models/test_fixture_generator.py`

**Interfaces:**
- Consumes: question, schema summary, metric context, fixture SQL.
- Produces: `SqlGenerationRequest`, `SqlGenerator`, and `FixtureSqlGenerator.generate(request) -> GeneratedSql`.

- [ ] **Step 1: Write a network-free fixture adapter test**

Create `tests/unit/models/test_fixture_generator.py`:

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

- [ ] **Step 2: Define the model-independent interface**

Create `protocols.py`:

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

- [ ] **Step 3: Freeze the baseline prompt**

`prompts.py` exports `BASELINE_SYSTEM_PROMPT_V1` with these requirements:

```text
You generate exactly one PostgreSQL read-only query for the supplied ecommerce question.
Use only tables and columns in SCHEMA CONTEXT and metric rules in METRIC CONTEXT.
Use half-open UTC time intervals. Do not invent columns or metrics.
Return one JSON object with keys "sql" and "assumptions".
The SQL must be one SELECT or WITH query. Do not include Markdown fences.
```

`build_baseline_user_prompt(request)` labels the question, schema context, and metric context in a stable order.

- [ ] **Step 4: Add twenty fixture responses**

`evals/fixtures/baseline_sql.json` maps every case ID to its Oracle SQL text. This fixture validates orchestration, safety, execution, scoring, and reporting; its score is never presented as live model quality.

- [ ] **Step 5: Run and commit model boundaries**

Run:

```bash
uv run pytest tests/unit/models/test_fixture_generator.py -v
uv run mypy src/governed_analytics/models
```

Expected: tests and typing pass with no network access.

```bash
git add src/governed_analytics/models evals/fixtures tests/unit/models
git commit -m "feat: 隔离基线模型接口与离线夹具"
```

## Task 4: Narrow Baseline SQL Guard and Executor

**Files:**
- Create: `src/governed_analytics/evals/sql_guard.py`
- Create: `src/governed_analytics/evals/executor.py`
- Create: `tests/unit/evals/test_sql_guard.py`
- Create: `tests/integration/evals/test_readonly_executor.py`

**Interfaces:**
- Consumes: model-generated SQL.
- Produces: `validate_baseline_sql(sql) -> str` and `execute_readonly_sql(sql) -> QueryResult`.

- [ ] **Step 1: Write guard allow/deny tests**

Create `tests/unit/evals/test_sql_guard.py`:

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

- [ ] **Step 2: Implement AST validation**

Parse with `sqlglot.parse(sql, read="postgres")`; require exactly one expression and require it to be an `exp.Query`. Reject any descendant of these classes:

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

Reject function names:

```python
DENIED_FUNCTIONS = frozenset(
    {"dblink", "lo_export", "lo_import", "pg_ls_dir", "pg_read_file", "pg_sleep", "set_config"}
)
```

Add or reduce the outer query limit to 500 using SQLGlot AST, then serialize in PostgreSQL dialect. Week 2 replaces this narrow guard with the full policy engine.

- [ ] **Step 3: Implement the read-only executor**

Use:

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

Create the engine exclusively from the readonly-only `DatabaseSettings.database_url`. Do not add
transaction state to the generic engine factory. The real PostgreSQL executor integration test
must query `current_setting` through this executor transaction and assert
`transaction_read_only = 'on'`, `statement_timeout = '10s'`, and
`search_path = 'public, pg_catalog'`.

- [ ] **Step 4: Prove database permissions backstop the guard**

Create an integration test that monkeypatches `validate_baseline_sql` to return `insert into categories ...` and asserts PostgreSQL raises `InsufficientPrivilege`. This proves a guard bypass still cannot write.

- [ ] **Step 5: Run and commit baseline execution safety**

Run:

```bash
uv run pytest tests/unit/evals/test_sql_guard.py -v
uv run pytest tests/integration/evals/test_readonly_executor.py -v
```

Expected: all allow/deny cases pass and the read-only database rejects DML.

```bash
git add src/governed_analytics/evals/sql_guard.py src/governed_analytics/evals/executor.py tests/unit/evals/test_sql_guard.py tests/integration/evals/test_readonly_executor.py
git commit -m "feat: 安全执行只读基线查询"
```

## Task 5: Result Scoring and Reports

**Files:**
- Create: `src/governed_analytics/evals/scorers.py`
- Create: `src/governed_analytics/evals/reporting.py`
- Create: `tests/unit/evals/test_scorers.py`
- Create: `tests/unit/evals/test_reporting.py`

**Interfaces:**
- Consumes: `GoldenCase`, expected `QueryResult`, actual `QueryResult`, and `BaselineCaseResult`.
- Produces: `score_result(...) -> Decimal`, aggregate `BaselineRunReport`, JSON and Markdown reports.

- [ ] **Step 1: Write exact scorer tests**

Cover these cases:

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

- [ ] **Step 2: Implement comparison semantics**

- scalar: one numeric value within absolute or relative tolerance;
- table: exact key set, exact nonnumeric cells, numeric tolerance per cell, row order ignored;
- top_k: key overlap divided by expected K, rounded to six decimals;
- boolean: exact normalized boolean match.

Case status is `passed` only when score equals 1. A valid partial `top_k` result remains `wrong_answer` with partial score.

- [ ] **Step 3: Implement aggregate metrics**

Calculate:

```python
result_accuracy = sum(case.score for case in cases) / Decimal(len(cases))
valid_sql_rate = valid_sql_cases / Decimal(len(cases))
execution_success_rate = executed_cases / Decimal(len(cases))
total_cost_cny = sum(case.estimated_cost_cny for case in cases)
```

Round displayed rates to four decimals and costs to six decimals; preserve unrounded per-case values in JSON.

- [ ] **Step 4: Generate immutable JSON and Markdown reports**

Directory format:

```text
artifacts/evals/baseline/<mode>/<YYYYMMDDTHHMMSSZ>-<run_id>/
├── report.json
├── report.md
└── cases/
    └── G001.json
```

Markdown includes dataset ID, prompt version, requested/resolved model, result accuracy, valid SQL rate, execution rate, total/average cost, P50/P95 latency, failure counts, and a 20-row case table. It explicitly labels fixture reports “harness validation, not model quality.”

- [ ] **Step 5: Run and commit scoring/reporting**

Run:

```bash
uv run pytest tests/unit/evals/test_scorers.py tests/unit/evals/test_reporting.py -v
```

Expected: scorer edge cases and snapshot-normalized report tests pass.

```bash
git add src/governed_analytics/evals/scorers.py src/governed_analytics/evals/reporting.py tests/unit/evals/test_scorers.py tests/unit/evals/test_reporting.py
git commit -m "feat: 评估基线结果并生成报告"
```

## Task 6: OpenAI-Compatible Live Adapter and Cost Accounting

**Files:**
- Create: `src/governed_analytics/models/openai_compatible.py`
- Create: `src/governed_analytics/evals/pricing.py`
- Create: `data/pricing/qwen3.7-plus-2026-09-01.yaml`
- Modify: `src/governed_analytics/config.py`
- Create: `tests/unit/models/test_openai_compatible.py`
- Create: `tests/unit/evals/test_pricing.py`

**Interfaces:**
- Consumes: `AsyncOpenAI`, `SqlGenerationRequest`, model settings, pricing YAML.
- Produces: `OpenAICompatibleSqlGenerator` and `estimate_cost_cny(...) -> Decimal`.

- [ ] **Step 1: Write a fake SDK response test**

Use a typed fake object whose `chat.completions.create` async method records arguments and returns content:

```json
{"sql":"select count(*) as order_count from orders","assumptions":["有效订单口径由指标目录提供"]}
```

Assert the adapter sends `model`, stable messages, `temperature=0`, `response_format={"type": "json_object"}`, and `max_completion_tokens=1200`; assert returned usage and provider model are preserved.

- [ ] **Step 2: Implement the adapter with client injection**

Add model settings to `src/governed_analytics/config.py`:

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

The CLI unwraps `model_api_key` only while constructing `AsyncOpenAI`; logs and reports never serialize the `SecretStr`.

Constructor:

```python
class OpenAICompatibleSqlGenerator:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model
```

Call the installed SDK surface:

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

Validate `response.choices[0].message.content` with a private Pydantic response model. Treat missing content, invalid JSON, or absent usage as categorized adapter errors; usage may be zero only in fixture mode.

- [ ] **Step 3: Version Qwen pricing metadata**

Create:

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

Cost formula uses `Decimal` only:

```python
cost = (
    Decimal(input_tokens) * input_price / Decimal(unit_tokens)
    + Decimal(output_tokens) * output_price / Decimal(unit_tokens)
)
```

- [ ] **Step 4: Run and commit the live adapter**

Run:

```bash
uv run pytest tests/unit/models/test_openai_compatible.py tests/unit/evals/test_pricing.py -v
```

Expected: fake client arguments, response parsing, and exact Decimal cost tests pass without a network request.

```bash
git add src/governed_analytics/config.py src/governed_analytics/models/openai_compatible.py src/governed_analytics/evals/pricing.py data/pricing tests/unit/models/test_openai_compatible.py tests/unit/evals/test_pricing.py
git commit -m "feat: 接入兼容模型并记录调用成本"
```

## Task 7: Baseline Runner, CLI, CI, and Documentation

**Files:**
- Create: `src/governed_analytics/evals/runner.py`
- Create: `src/governed_analytics/evals/cli.py`
- Create: `tests/integration/evals/test_baseline_runner.py`
- Modify: `pyproject.toml`
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`
- Create: `docs/evals.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: cases, schema/metric context, fixture or live generator, guard, executor, Oracle results, scorers, pricing.
- Produces: `governed-eval baseline` and complete baseline reports.

- [ ] **Step 1: Add the console entry point**

Add:

```toml
[project.scripts]
governed-data = "governed_analytics.data_generation.cli:main"
governed-eval = "governed_analytics.evals.cli:main"
```

- [ ] **Step 2: Write the fixture runner integration test**

Create `tests/integration/evals/test_baseline_runner.py`:

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

- [ ] **Step 3: Implement one-pass case execution**

For each case, exactly once:

1. create `SqlGenerationRequest`;
2. call `generator.generate` once;
3. validate SQL once;
4. execute SQL once;
5. compare to committed Oracle result;
6. record result, latency, usage, cost, and any failure category.

Catch and classify only at the case boundary. Continue to the next case after a failure. Do not retry or repair SQL in Week 1.

- [ ] **Step 4: Enforce live authorization in the CLI**

CLI surface:

```text
governed-eval baseline --dataset tiny --mode fixture
governed-eval baseline --dataset tiny --mode live --live
```

If mode is live and `--live` is absent, exit 2 with `Live model calls require --live`. If the key is absent, exit 2 with `MODEL_API_KEY is not configured`. Reject full dataset baseline in Week 1.

- [ ] **Step 5: Add fixture-only Make and CI commands**

```make
.PHONY: eval-fixture

eval-fixture:
	@uv run governed-eval baseline --dataset tiny --mode fixture
```

Add to CI after tiny data/metric checks:

```yaml
      - run: uv run governed-eval baseline --dataset tiny --mode fixture
```

Do not add `MODEL_API_KEY` to CI and do not run live mode.

- [ ] **Step 6: Document report interpretation and OpenAI-compatible boundary**

`docs/evals.md` documents case schema, Oracle materialization, scorer semantics, failure categories, fixture/live distinction, price snapshot, explicit live authorization, and the fact that live results are the baseline—not fixture 100%.

Reference the official Chat Completions surface used by the adapter: `https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions`.

- [ ] **Step 7: Run the complete Plan C gate**

Run:

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

Expected: twenty fixture cases pass, no external call occurs, all quality checks pass, and reports are generated under ignored artifacts.

- [ ] **Step 8: Commit Plan C workflow**

```bash
git add pyproject.toml uv.lock src/governed_analytics/evals src/governed_analytics/models Makefile .github/workflows/ci.yml docs/evals.md README.md
git commit -m "feat: 建立可复现 Text-to-SQL 基线"
```

## Plan C Completion Gate

- [ ] Twenty case definitions, twenty Oracle SQL files, and twenty expected JSON files are versioned.
- [ ] Exact anomaly cases return seeded counts `20`, `10`, and `3` for G018-G020 on tiny data.
- [ ] Fixture mode executes all twenty cases with zero tokens, zero CNY cost, and no network.
- [ ] Unsafe/multiple SQL is rejected before execution; PostgreSQL permissions independently reject DML.
- [ ] Reports include accuracy, valid SQL rate, execution rate, P50/P95 latency, tokens, cost, and all failures.
- [ ] Live calls require explicit double authorization and never run in CI.
- [ ] The first authorized live report is retained even if its accuracy is poor.
- [ ] No LangGraph or repair loop is present, preserving the value of this baseline comparison.
