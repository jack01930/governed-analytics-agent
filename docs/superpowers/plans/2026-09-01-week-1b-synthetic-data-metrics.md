# Week 1B Synthetic Data and Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and load a deterministic ecommerce dataset at tiny and full scales, publish machine-readable anomaly truth, and validate the first fifteen versioned metric definitions.

**Architecture:** Pure generator functions create canonical CSV files without database access. Named random streams make each table stable even when another generator changes. A separate loader uses PostgreSQL `COPY` through `analytics_loader`, computes database digests, and writes a manifest; metric YAML is parsed into immutable Pydantic contracts and validated independently.

**Tech Stack:** Python 3.12, NumPy, Pandas, Pydantic 2, PyYAML, psycopg 3, PostgreSQL 17, SQLGlot, Pytest.

**Spec:** `GOVERNED_ANALYTICS_AGENT_PLAN.md` sections 18-23 and `docs/superpowers/plans/2026-09-01-week-1-data-baseline.md`.

## Global Constraints

- Fixed seed is `20260901`; never use module-global `random` or NumPy RNG state.
- Data interval is `[2025-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`.
- Tiny scale is committed only as configuration and small expected fixtures; generated CSV files stay under ignored `artifacts/`.
- Full scale targets 50,000 customers, 2,000 products, 300,000 orders, 1,200,000 web sessions, 30 campaigns, and daily inventory snapshots.
- Money is generated as integer cents and serialized with exactly two decimal places.
- CSV columns and row ordering are fixed per table. Digests use canonical UTF-8 CSV bytes with LF endings.
- Every anomaly is deterministic, has an ID, time window, affected keys, root cause, mutation parameters, and expected observable signal.
- Metric definitions are semantic metadata, not executable arbitrary templates. SQL is parsed before acceptance.
- The generator does not call a model, network service, or external data source.

---

## Fixed Business Vocabulary

Channels: `organic`, `search`, `social`, `affiliate`, `email`.

Customer segments: `new`, `regular`, `vip`.

Regions, in stable order:

```python
REGIONS = (
    "北京", "上海", "广州", "深圳", "杭州", "南京",
    "成都", "重庆", "武汉", "西安", "苏州", "天津",
    "长沙", "郑州", "青岛", "宁波", "佛山", "东莞",
    "厦门", "福州", "南宁", "海口", "昆明", "合肥",
)
SOUTH_REGIONS = frozenset({"广州", "深圳", "佛山", "东莞", "厦门", "福州", "南宁", "海口"})
```

Category codes are `CAT-001` through `CAT-020`. Product SKUs are `SKU-000001` upward. Customer, order, payment, refund, session, and campaign codes use fixed zero-padded numeric suffixes.

## Fixed Scale Configurations

Create `data/generator/tiny.yaml`:

```yaml
scale: tiny
seed: 20260901
start_at: 2025-01-01T00:00:00Z
end_at: 2026-07-01T00:00:00Z
customers: 500
products: 100
orders: 3000
campaigns: 6
```

Create `data/generator/full.yaml`:

```yaml
scale: full
seed: 20260901
start_at: 2025-01-01T00:00:00Z
end_at: 2026-07-01T00:00:00Z
customers: 50000
products: 2000
orders: 300000
campaigns: 30
```

Derived counts are deterministic:

| Table | Tiny | Full | Rule |
|---|---:|---:|---|
| categories | 20 | 20 | fixed |
| order_items | approximately 6,300 | approximately 630,000 | 1-5 items, mean 2.1 |
| payments | 3,000 | 300,000 | one attempt per order |
| refunds | approximately 180 | approximately 18,000 | base successful refund probability 6% |
| web_sessions | 15,000 | 1,200,000 | fixed 5x orders for tiny, 4x for full |
| inventory_snapshots | 54,600 | 1,092,000 | one daily row per product for 546 days |
| pipeline_runs | 1,638 | 1,638 | three daily pipelines for 546 days |

Approximate rows are recorded as actual counts in `DatasetManifest`; tests assert deterministic equality, not the approximate number.

## Seeded Anomaly Contract

| ID | Window | Mutation | Expected signal |
|---|---|---|---|
| `anomaly_gmv_drop_south_conversion` | 2026-06-08 to 2026-06-15 | multiply conversion probability by `0.65` for `SOUTH_REGIONS` | South GMV and converted sessions fall versus 2026-06-01 to 2026-06-08 |
| `anomaly_gmv_drop_stockout` | 2026-06-08 to 2026-06-15 | set available quantity to zero for `SKU-000001` and `SKU-000002`; suppress their order items by `0.90` | two products contribute materially to GMV loss |
| `anomaly_refund_spike_category` | 2026-05-04 to 2026-05-11 | raise successful refund probability from `0.06` to `0.24` for `CAT-018` | category refund rate rises at least 2x |
| `anomaly_inventory_delay` | 2026-06-15 | omit inventory snapshot rows after 08:00 UTC and create failed pipeline run | freshness rule finds a stale watermark |
| `anomaly_duplicate_order_items` | 2026-04-10 | copy 20 tiny / 2,000 full order item rows with the same `source_line_id` and new PK | logical duplicate count is exact |
| `anomaly_order_amount_mismatch` | 2026-03-17 | add CNY 10.00 to `orders.payable_amount` for 10 tiny / 1,000 full orders | order-to-item reconciliation fails |
| `anomaly_missing_region` | 2026-02-12 | set `orders.region` null for 10 tiny / 1,000 full orders | completeness rule finds exact null count |
| `anomaly_refund_exceeds_payment` | 2026-05-20 | add CNY 50.00 above successful payment to 3 tiny / 300 full refunds | refund consistency rule finds exact violations |

## Task 1: Generator Contracts and Named Random Streams

**Files:**
- Modify: `pyproject.toml`
- Create: `data/generator/tiny.yaml`
- Create: `data/generator/full.yaml`
- Create: `src/governed_analytics/data_generation/__init__.py`
- Create: `src/governed_analytics/data_generation/models.py`
- Create: `src/governed_analytics/data_generation/randomness.py`
- Create: `tests/unit/data_generation/test_models.py`
- Create: `tests/unit/data_generation/test_randomness.py`

**Interfaces:**
- Consumes: the fixed scale YAML above.
- Produces: `DatasetScale`, `GeneratorConfig`, `TableDigest`, `DatasetManifest`, and `named_rng(seed, namespace) -> numpy.random.Generator`.

- [ ] **Step 1: Declare direct dependencies and console entry point**

Add direct dependencies `numpy` and `pyyaml` to `[project].dependencies`; do not rely on transitive installation. Add:

```toml
[project.scripts]
governed-data = "governed_analytics.data_generation.cli:main"
```

Run `uv lock` after the file exists; commit the resulting `uv.lock` with Task 1.

- [ ] **Step 2: Write failing model and RNG tests**

Create `tests/unit/data_generation/test_randomness.py`:

```python
import numpy as np

from governed_analytics.data_generation.randomness import named_rng


def test_named_stream_is_repeatable_and_namespace_isolated() -> None:
    first = named_rng(20260901, "orders").integers(0, 10_000, size=10)
    second = named_rng(20260901, "orders").integers(0, 10_000, size=10)
    customers = named_rng(20260901, "customers").integers(0, 10_000, size=10)

    np.testing.assert_array_equal(first, second)
    assert not np.array_equal(first, customers)
```

Create `tests/unit/data_generation/test_models.py`:

```python
from pathlib import Path

import yaml

from governed_analytics.data_generation.models import DatasetScale, GeneratorConfig


def test_tiny_config_is_frozen_and_exact() -> None:
    raw = yaml.safe_load(Path("data/generator/tiny.yaml").read_text(encoding="utf-8"))
    config = GeneratorConfig.model_validate(raw)

    assert config.scale is DatasetScale.TINY
    assert config.seed == 20260901
    assert config.customers == 500
    assert config.products == 100
    assert config.orders == 3000
    assert config.start_at.tzinfo is not None
```

- [ ] **Step 3: Run tests and verify missing modules**

Run:

```bash
uv run pytest tests/unit/data_generation/test_models.py tests/unit/data_generation/test_randomness.py -v
```

Expected: imports fail because the data-generation modules do not exist.

- [ ] **Step 4: Implement the immutable contracts**

Create `models.py` with the exact cross-plan classes from `2026-09-01-week-1-data-baseline.md`. Add this validator to `GeneratorConfig`:

```python
from typing import Self

from pydantic import model_validator


@model_validator(mode="after")
def validate_interval(self) -> Self:
    if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
        raise ValueError("start_at and end_at must be timezone-aware")
    if self.end_at <= self.start_at:
        raise ValueError("end_at must be after start_at")
    return self
```

Create `randomness.py`:

```python
from hashlib import sha256

import numpy as np
from numpy.random import Generator


def named_rng(seed: int, namespace: str) -> Generator:
    digest = sha256(f"{seed}:{namespace}".encode()).digest()
    namespace_seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return np.random.default_rng(namespace_seed)
```

- [ ] **Step 5: Run tests and commit contracts**

Run:

```bash
uv lock
uv run pytest tests/unit/data_generation/test_models.py tests/unit/data_generation/test_randomness.py -v
```

Expected: all tests pass.

```bash
git add pyproject.toml uv.lock data/generator src/governed_analytics/data_generation tests/unit/data_generation
git commit -m "feat: 定义可复现数据生成契约"
```

## Task 2: Canonical Dimension Generators

**Files:**
- Create: `src/governed_analytics/data_generation/vocabulary.py`
- Create: `src/governed_analytics/data_generation/dimensions.py`
- Create: `tests/unit/data_generation/test_dimensions.py`

**Interfaces:**
- Consumes: `GeneratorConfig` and named streams `customers`, `products`, `campaigns`.
- Produces: `generate_categories()`, `generate_customers(config)`, `generate_products(config)`, and `generate_campaigns(config)`, each returning a DataFrame with schema order matching PostgreSQL.

- [ ] **Step 1: Write deterministic dimension tests**

Create `tests/unit/data_generation/test_dimensions.py`:

```python
from governed_analytics.data_generation.dimensions import (
    generate_campaigns,
    generate_categories,
    generate_customers,
    generate_products,
)
from governed_analytics.data_generation.models import load_generator_config


def test_tiny_dimensions_have_stable_keys_and_counts() -> None:
    config = load_generator_config("data/generator/tiny.yaml")

    categories = generate_categories()
    customers = generate_customers(config)
    products = generate_products(config)
    campaigns = generate_campaigns(config)

    assert categories["category_code"].tolist() == [f"CAT-{i:03d}" for i in range(1, 21)]
    assert len(customers) == 500
    assert customers.iloc[0]["customer_code"] == "CUS-000001"
    assert len(products) == 100
    assert products.iloc[-1]["sku"] == "SKU-000100"
    assert len(campaigns) == 6
```

- [ ] **Step 2: Run and verify missing generator functions**

Run:

```bash
uv run pytest tests/unit/data_generation/test_dimensions.py -v
```

Expected: import fails for `dimensions` or `load_generator_config`.

- [ ] **Step 3: Implement fixed vocabulary and config loading**

Create `vocabulary.py` with the exact regions and channel tuples in this plan plus 20 stable Chinese category names. Add to `models.py`:

```python
from pathlib import Path

import yaml


def load_generator_config(path: str | Path) -> GeneratorConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GeneratorConfig.model_validate(raw)
```

- [ ] **Step 4: Implement dimension generation rules**

Use these exact rules in `dimensions.py`:

- categories: stable IDs 1-20 and codes `CAT-001` to `CAT-020`;
- customers: sequential IDs and codes, region sampled from fixed weights, segment weights `new=0.25`, `regular=0.60`, `vip=0.15`, registration uniformly before `end_at`;
- products: sequential IDs/SKUs, category sampled uniformly, list price sampled log-normally then clamped to CNY 19.00-4,999.00, cost ratio sampled 0.35-0.75;
- campaigns: sequential IDs/codes, channel from non-organic channels, 14-day duration, non-overlapping start dates, spend CNY 5,000-100,000.

Convert cents with:

```python
from decimal import Decimal


def cents_to_money(cents: int) -> str:
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")))
```

- [ ] **Step 5: Run, check types, and commit dimensions**

Run:

```bash
uv run pytest tests/unit/data_generation/test_dimensions.py -v
uv run mypy src/governed_analytics/data_generation
```

Expected: tests and type checking pass.

```bash
git add src/governed_analytics/data_generation tests/unit/data_generation/test_dimensions.py
git commit -m "feat: 生成确定性业务维表"
```

## Task 3: Fact Generators and Business Seasonality

**Files:**
- Create: `src/governed_analytics/data_generation/facts.py`
- Create: `tests/unit/data_generation/test_facts.py`

**Interfaces:**
- Consumes: generated dimensions and named random streams.
- Produces: base, pre-anomaly DataFrames for orders, items, payments, refunds, sessions, inventory, attributions, and pipeline runs.

- [ ] **Step 1: Write a tiny fact contract test**

Create `tests/unit/data_generation/test_facts.py`:

```python
from governed_analytics.data_generation.facts import generate_base_facts
from governed_analytics.data_generation.models import load_generator_config


def test_base_facts_are_referentially_valid_and_repeatable() -> None:
    config = load_generator_config("data/generator/tiny.yaml")
    first = generate_base_facts(config)
    second = generate_base_facts(config)

    assert len(first.orders) == 3000
    assert len(first.web_sessions) == 15000
    assert first.orders.equals(second.orders)
    assert set(first.order_items["order_id"]) <= set(first.orders["order_id"])
    assert set(first.payments["order_id"]) == set(first.orders["order_id"])
    assert first.orders["ordered_at"].min() >= config.start_at
    assert first.orders["ordered_at"].max() < config.end_at
```

- [ ] **Step 2: Run and verify the missing fact module**

Run:

```bash
uv run pytest tests/unit/data_generation/test_facts.py -v
```

Expected: import fails for `facts`.

- [ ] **Step 3: Implement `GeneratedFacts` and timestamp distribution**

Create an immutable container:

```python
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class GeneratedFacts:
    orders: pd.DataFrame
    order_items: pd.DataFrame
    payments: pd.DataFrame
    refunds: pd.DataFrame
    web_sessions: pd.DataFrame
    inventory_snapshots: pd.DataFrame
    campaign_attributions: pd.DataFrame
    pipeline_runs: pd.DataFrame
```

Generate order timestamps with weights:

- December multiplier `1.25`;
- June multiplier `0.92`;
- Friday-Sunday multiplier `1.15`;
- all other dates multiplier `1.0`;
- normalize weights before sampling.

- [ ] **Step 4: Implement exact fact rules**

- one payment per order; success probabilities: paid/completed/refunded `0.97`, cancelled `0.05`, placed `0.30`;
- order status weights before payment reconciliation: placed `0.03`, paid `0.12`, completed `0.77`, cancelled `0.08`;
- 1-5 order items with probabilities `[0.35, 0.35, 0.18, 0.08, 0.04]`;
- item quantity 1-3 with probabilities `[0.78, 0.17, 0.05]`;
- item discount rate sampled from `[0, 0.05, 0.10, 0.15]` with probabilities `[0.55, 0.20, 0.20, 0.05]`;
- successful refund base probability `0.06`, amount limited to item net amount before anomaly mutation;
- sessions: tiny `orders * 5`, full `orders * 4`; channel weights `[0.30, 0.25, 0.20, 0.10, 0.15]`; converted sessions reference an order;
- daily inventory snapshot per product with baseline quantity 0-500 and replenishment noise;
- attributions only for orders within campaign window, at most one row per campaign/order pair;
- three pipeline rows per day: `orders`, `inventory`, and `sessions`, normally succeeded with watermark equal to the business date end.

Ensure every DataFrame is sorted by its identity column before return.

- [ ] **Step 5: Run and commit fact generation**

Run:

```bash
uv run pytest tests/unit/data_generation/test_facts.py -v
uv run ruff check src/governed_analytics/data_generation/facts.py
uv run mypy src/governed_analytics/data_generation/facts.py
```

Expected: all commands pass.

```bash
git add src/governed_analytics/data_generation/facts.py tests/unit/data_generation/test_facts.py
git commit -m "feat: 生成订单与行为事实数据"
```

## Task 4: Anomaly Injection and Truth Manifest

**Files:**
- Create: `src/governed_analytics/data_generation/anomalies.py`
- Create: `data/manifests/anomaly_manifest.schema.json`
- Create: `tests/unit/data_generation/test_anomalies.py`

**Interfaces:**
- Consumes: `GeneratedFacts`, dimensions, scale, and the anomaly contract table.
- Produces: mutated facts and `AnomalyManifest` serialized as `anomaly_manifest.json`.

- [ ] **Step 1: Write exact anomaly-count tests**

Create `tests/unit/data_generation/test_anomalies.py`:

```python
from governed_analytics.data_generation.anomalies import inject_anomalies
from governed_analytics.data_generation.facts import generate_base_facts
from governed_analytics.data_generation.models import load_generator_config


def test_tiny_quality_anomalies_have_exact_counts() -> None:
    config = load_generator_config("data/generator/tiny.yaml")
    base = generate_base_facts(config)
    result = inject_anomalies(config, base)

    assert result.order_items["source_line_id"].duplicated(keep=False).sum() == 40
    assert result.orders["region"].isna().sum() == 10
    assert result.manifest.by_id("anomaly_duplicate_order_items").mutated_rows == 20
    assert result.manifest.by_id("anomaly_order_amount_mismatch").mutated_rows == 10
    assert result.manifest.by_id("anomaly_refund_exceeds_payment").mutated_rows == 3
```

- [ ] **Step 2: Run and verify the missing anomaly module**

Run:

```bash
uv run pytest tests/unit/data_generation/test_anomalies.py -v
```

Expected: import fails for `anomalies`.

- [ ] **Step 3: Define anomaly models and JSON Schema**

Implement:

```python
class ExpectedSignal(BaseModel):
    metric_id: str
    operator: Literal["decrease", "increase", "equals", "stale"]
    threshold: Decimal | int | str


class AnomalyRecord(BaseModel):
    anomaly_id: str
    start_at: datetime
    end_at: datetime
    root_cause: str
    affected_keys: tuple[str, ...]
    mutation: dict[str, str | int | float]
    mutated_rows: int
    expected_signals: tuple[ExpectedSignal, ...]


class AnomalyManifest(BaseModel):
    dataset_id: str
    anomalies: tuple[AnomalyRecord, ...]

    def by_id(self, anomaly_id: str) -> AnomalyRecord:
        return next(item for item in self.anomalies if item.anomaly_id == anomaly_id)
```

Generate `data/manifests/anomaly_manifest.schema.json` from `AnomalyManifest.model_json_schema()` and commit it. A test must compare the committed schema to the generated schema.

- [ ] **Step 4: Implement all eight deterministic mutations**

Implement the anomaly contract at the top of this plan exactly. Select rows by stable ascending primary key after applying the time/scope filter; never sample anomaly rows randomly.

For the GMV conversion anomaly, deterministically flip converted South sessions from `true` to `false`, clear their `order_id`, set the previously linked orders to `cancelled`, and set their payments to `failed` with null `paid_at`. This preserves order records while removing them from valid-order GMV.

For stockout, remove 90% of affected SKU order items in the anomaly window and recompute order/payment totals. If an order loses its final item, retain the order but set it to `cancelled`, set monetary totals to zero, and mark its payment failed. These rules keep configured order counts stable while making both root causes observable in GMV.

- [ ] **Step 5: Run tests and commit anomaly truth**

Run:

```bash
uv run pytest tests/unit/data_generation/test_anomalies.py -v
uv run mypy src/governed_analytics/data_generation/anomalies.py
```

Expected: exact tiny anomaly counts pass and the manifest schema is stable.

```bash
git add src/governed_analytics/data_generation/anomalies.py data/manifests tests/unit/data_generation/test_anomalies.py
git commit -m "feat: 注入可验证业务与质量异常"
```

## Task 5: Canonical CSV Writer, PostgreSQL Loader, and Digests

**Files:**
- Create: `src/governed_analytics/data_generation/writer.py`
- Create: `src/governed_analytics/data_generation/loader.py`
- Create: `src/governed_analytics/data_generation/pipeline.py`
- Create: `tests/unit/data_generation/test_writer.py`
- Create: `tests/integration/data_generation/test_pipeline.py`

**Interfaces:**
- Consumes: generated dimensions, mutated facts, `LOADER_DATABASE_URL`.
- Produces: canonical CSV files, database rows, and `DatasetManifest` with per-table counts/digests.

- [ ] **Step 1: Write a canonical digest test**

Create `tests/unit/data_generation/test_writer.py`:

```python
from pathlib import Path

import pandas as pd

from governed_analytics.data_generation.writer import write_canonical_csv


def test_canonical_csv_is_stable_across_input_order(tmp_path: Path) -> None:
    first = pd.DataFrame([{"id": 2, "value": "b"}, {"id": 1, "value": "a"}])
    second = first.iloc[::-1].reset_index(drop=True)

    first_digest = write_canonical_csv(first, tmp_path / "first.csv", sort_by=("id",))
    second_digest = write_canonical_csv(second, tmp_path / "second.csv", sort_by=("id",))

    assert first_digest == second_digest
    assert (tmp_path / "first.csv").read_bytes() == (tmp_path / "second.csv").read_bytes()
```

- [ ] **Step 2: Implement canonical writing**

`write_canonical_csv` must:

```python
def write_canonical_csv(frame: pd.DataFrame, path: Path, *, sort_by: tuple[str, ...]) -> str:
    canonical = frame.sort_values(list(sort_by), kind="stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical.to_csv(path, index=False, lineterminator="\n", date_format="%Y-%m-%dT%H:%M:%SZ")
    return sha256(path.read_bytes()).hexdigest()
```

- [ ] **Step 3: Implement a static COPY registry and loader**

Define `TABLE_LOAD_ORDER` and exact CSV column tuples in `loader.py`. Accept only names from this registry. Load in this order:

```python
TABLE_LOAD_ORDER = (
    "categories",
    "customers",
    "products",
    "marketing_campaigns",
    "orders",
    "order_items",
    "payments",
    "refunds",
    "inventory_snapshots",
    "web_sessions",
    "campaign_attributions",
    "pipeline_runs",
)
```

Use psycopg COPY:

```python
with connection.cursor().copy(
    f"copy {table_name} ({', '.join(columns)}) from stdin with (format csv, header true)"
) as copy:
    with csv_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            copy.write(chunk)
```

The f-string is safe only because `table_name` and `columns` come from the static registry, never from CLI input.

- [ ] **Step 4: Write and pass a tiny pipeline integration test**

Create `tests/integration/data_generation/test_pipeline.py`:

```python
from pathlib import Path

import pytest

from governed_analytics.data_generation.pipeline import generate_and_load


@pytest.mark.integration
def test_tiny_pipeline_is_reproducible(tmp_path: Path) -> None:
    first = generate_and_load("data/generator/tiny.yaml", tmp_path / "first")
    second = generate_and_load("data/generator/tiny.yaml", tmp_path / "second")

    assert first.config_sha256 == second.config_sha256
    assert [(item.table_name, item.row_count, item.sha256) for item in first.tables] == [
        (item.table_name, item.row_count, item.sha256) for item in second.tables
    ]
    assert first.dataset_id == second.dataset_id
```

Before the second load, `generate_and_load` truncates only the twelve known tables using the loader connection and restarts identities. It must reject database names or table names from user input.

- [ ] **Step 5: Run and commit the load pipeline**

Run:

```bash
uv run pytest tests/unit/data_generation/test_writer.py -v
uv run pytest tests/integration/data_generation/test_pipeline.py -v
```

Expected: canonical digest test and two-load database reproducibility test pass.

```bash
git add src/governed_analytics/data_generation tests/unit/data_generation tests/integration/data_generation
git commit -m "feat: 加载并校验可复现模拟数据"
```

## Task 6: First Fifteen Metric Definitions

**Files:**
- Create: `src/governed_analytics/domain/metrics.py`
- Create: `src/governed_analytics/metrics/__init__.py`
- Create: `src/governed_analytics/metrics/catalog.py`
- Create: `data/metrics/core.yaml`
- Create: `tests/unit/metrics/test_catalog.py`
- Create: `tests/integration/metrics/test_metric_queries.py`

**Interfaces:**
- Consumes: loaded database schema and fixed business definitions.
- Produces: `MetricDefinition`, `load_metric_catalog(path)`, `get_metric(metric_id)`, and fifteen parsed definitions.

- [ ] **Step 1: Write catalog validation tests**

Create `tests/unit/metrics/test_catalog.py`:

```python
from governed_analytics.metrics.catalog import load_metric_catalog


def test_core_catalog_has_exact_metric_ids_and_valid_sql() -> None:
    catalog = load_metric_catalog("data/metrics/core.yaml")

    assert tuple(catalog) == (
        "gmv", "paid_gmv", "net_revenue", "valid_order_count", "average_order_value",
        "payment_success_rate", "refund_amount", "refund_rate", "active_customers",
        "new_customers", "repeat_purchase_rate", "session_count", "conversion_rate",
        "stockout_rate", "campaign_roi",
    )
```

The loader must reject duplicate IDs, unknown source tables, timezone-naive `valid_from`, and expressions SQLGlot cannot parse inside `select <expression>`.

- [ ] **Step 2: Define exact metric semantics in YAML**

Create `data/metrics/core.yaml` with version `1.0.0`, `valid_from: 2025-01-01T00:00:00Z`, and these formulas:

| Metric ID | Expression | Time field | Unit | Allowed dimensions |
|---|---|---|---|---|
| `gmv` | `sum(oi.net_amount)` for order status `paid, completed, refunded` | `o.ordered_at` | CNY | region, channel, category, product, segment |
| `paid_gmv` | `sum(p.amount)` where payment succeeded | `p.paid_at` | CNY | region, channel, segment |
| `net_revenue` | successful payment amount minus successful refund amount | `o.ordered_at` | CNY | region, channel, category, product |
| `valid_order_count` | distinct orders with status `paid, completed, refunded` | `o.ordered_at` | count | region, channel, segment |
| `average_order_value` | `gmv / nullif(valid_order_count, 0)` | `o.ordered_at` | CNY | region, channel, segment |
| `payment_success_rate` | succeeded payment attempts / all payment attempts | `p.created_at` | ratio | provider, channel |
| `refund_amount` | successful refund amount | `r.refunded_at` | CNY | reason, category, product, region |
| `refund_rate` | successful refund amount / successful payment amount for matching orders | `o.ordered_at` | ratio | category, product, region |
| `active_customers` | distinct customers with valid orders | `o.ordered_at` | count | region, segment |
| `new_customers` | customers registered in interval | `c.registered_at` | count | region, segment |
| `repeat_purchase_rate` | customers with at least two valid lifetime orders by interval end / active customers | `o.ordered_at` | ratio | region, segment |
| `session_count` | count of web sessions | `s.occurred_at` | count | channel, region |
| `conversion_rate` | converted sessions / all sessions | `s.occurred_at` | ratio | channel, region |
| `stockout_rate` | product snapshots with available quantity zero / all product snapshots | `i.snapshot_at` | ratio | category, product |
| `campaign_roi` | `(attributed_revenue - campaign_spend) / nullif(campaign_spend, 0)` | `a.attributed_at` | ratio | campaign, channel |

Each YAML entry includes description, default filters, source table list, and one example question. Ratios are stored as 0-1, not percentages.

- [ ] **Step 3: Implement immutable metric contracts and loader**

Implement the exact `MetricDefinition` from the master plan. `load_metric_catalog` returns `dict[str, MetricDefinition]` preserving file order. Validate expressions with:

```python
sqlglot.parse_one(f"select {metric.expression_sql}", read="postgres")
```

The catalog loader validates metadata only; it does not concatenate user input into executable SQL.

- [ ] **Step 4: Add database sanity queries for all fifteen metrics**

Create integration tests that execute one fixed, parameterized query per metric for `[2026-06-01, 2026-07-01)`. Assert:

- money and counts are nonnegative;
- ratios are either null for a zero denominator or in `[0, 1]`, except `campaign_roi`, which may be negative;
- every query completes under `statement_timeout = '10s'` using `analytics_readonly`.

- [ ] **Step 5: Run and commit the metric catalog**

Run:

```bash
uv run pytest tests/unit/metrics -v
uv run pytest tests/integration/metrics -v
```

Expected: fifteen definitions parse and all fixed validation queries complete.

```bash
git add data/metrics src/governed_analytics/domain/metrics.py src/governed_analytics/metrics tests/unit/metrics tests/integration/metrics
git commit -m "feat: 定义首批十五个业务指标"
```

## Task 7: CLI, CI, and Data Documentation

**Files:**
- Create: `src/governed_analytics/data_generation/cli.py`
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`
- Create: `docs/data-generation.md`
- Create: `docs/metrics.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: generator pipeline and metric catalog.
- Produces: `governed-data generate`, `governed-data verify`, `make data-tiny`, and `make metrics-check`.

- [ ] **Step 1: Implement CLI commands with explicit scale choices**

Use `argparse` with this surface:

```text
governed-data generate --scale {tiny,full} [--output artifacts/datasets]
governed-data verify --scale {tiny,full} [--output artifacts/datasets]
```

`generate` loads data and writes manifests. `verify` regenerates into a temporary artifact directory, compares config/table digests, prints differing table names, and exits 1 on mismatch.

- [ ] **Step 2: Add Make targets**

```make
.PHONY: data-tiny data-verify metrics-check

data-tiny:
	@uv run governed-data generate --scale tiny

data-verify:
	@uv run governed-data verify --scale tiny

metrics-check:
	@uv run pytest tests/unit/metrics tests/integration/metrics -v
```

- [ ] **Step 3: Extend CI with tiny generation only**

After migrations and persistence tests in the database job, add:

```yaml
      - run: uv run governed-data generate --scale tiny
      - run: uv run governed-data verify --scale tiny
      - run: uv run pytest tests/unit/metrics tests/integration/metrics -v
```

CI must never generate the full dataset.

- [ ] **Step 4: Document reproducibility and metric semantics**

`docs/data-generation.md` documents scale counts, seed, named RNGs, all eight anomalies, artifacts, manifests, and safe regeneration. `docs/metrics.md` documents the 15 metric IDs, formulas, time fields, dimensions, and ratio representation.

- [ ] **Step 5: Run the complete Plan B gate**

Run:

```bash
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check
make check
git diff --check
```

Expected: generation and verification complete with identical digests; fifteen metric tests and quality checks pass.

- [ ] **Step 6: Commit Plan B workflow**

```bash
git add src/governed_analytics/data_generation/cli.py Makefile .github/workflows/ci.yml docs/data-generation.md docs/metrics.md README.md
git commit -m "docs: 固化数据与指标复现流程"
```

## Plan B Completion Gate

- [ ] Tiny generation completes within 60 seconds on the development machine.
- [ ] Full generation completes within 15 minutes and uses chunked COPY rather than row-by-row INSERT.
- [ ] Two runs with seed `20260901` have identical per-table counts and SHA-256 digests.
- [ ] Eight anomaly records match exact tiny mutation counts and expected signals.
- [ ] All foreign keys remain valid after anomaly injection.
- [ ] Fifteen metric definitions parse, validate, and execute fixed sanity queries.
- [ ] No generated CSV or database volume is staged by Git.
- [ ] CI executes tiny generation and no external API request.
