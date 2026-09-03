# 第 1B 周：合成数据与指标实现计划

> **状态同步（2026-09-04）：** 实现步骤已完成；tiny/full 数据、8 类异常、证据清单与 15 个指标均已生成，
> 且 tiny/full 产物校验通过。外部 CI 与 full 重新生成耗时分别保留为外部/性能复核项。

> **供 Agent 执行者使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务实施本计划。各步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 生成并加载 tiny 与 full 两种规模的确定性电商数据集，发布机器可读的异常真值，并验证首批 15 个版本化指标定义。

**架构：** 纯生成函数在不访问数据库的情况下创建规范 CSV。命名随机流保证即使其他生成器发生变化，每张表仍保持稳定。独立加载器通过 `analytics_loader` 使用 PostgreSQL `COPY`，计算数据库摘要并写入清单；指标 YAML 被解析为不可变 Pydantic 契约并独立验证。

**技术栈：** Python 3.12、NumPy、Pandas、Pydantic 2、PyYAML、psycopg 3、PostgreSQL 17、SQLGlot、Pytest。

**规格依据：** `GOVERNED_ANALYTICS_AGENT_PLAN.md` 第 18–23 节，以及 `docs/superpowers/plans/2026-09-01-week-1-data-baseline.md`。

## 全局约束

- 固定种子为 `20260901`；不得使用模块全局 `random` 或 NumPy RNG 状态。
- 数据区间为 `[2025-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`。
- tiny 规模只提交配置和小型预期 fixture；生成的 CSV 始终位于被忽略的 `artifacts/` 下。
- full 规模目标为 50,000 个客户、2,000 个商品、300,000 个订单、1,200,000 个 Web 会话、30 个营销活动及每日库存快照。
- 金额以整数分生成，并严格序列化为两位小数。
- 每张表的 CSV 列与行顺序固定。摘要使用以 LF 结尾的规范 UTF-8 CSV 字节。
- 每个异常均为确定性，包含 ID、时间窗口、受影响键、根因、变异参数及预期可观测信号。
- 指标定义是语义元数据，不是可执行的任意模板；接收前必须解析 SQL。
- 生成器不调用模型、网络服务或外部数据源。

---

## 固定业务词汇

渠道：`organic`、`search`、`social`、`affiliate`、`email`。

客户分群：`new`、`regular`、`vip`。

地区按以下稳定顺序排列：

```python
REGIONS = (
    "北京", "上海", "广州", "深圳", "杭州", "南京",
    "成都", "重庆", "武汉", "西安", "苏州", "天津",
    "长沙", "郑州", "青岛", "宁波", "佛山", "东莞",
    "厦门", "福州", "南宁", "海口", "昆明", "合肥",
)
SOUTH_REGIONS = frozenset({"广州", "深圳", "佛山", "东莞", "厦门", "福州", "南宁", "海口"})
```

品类编码为 `CAT-001` 至 `CAT-020`。商品 SKU 从 `SKU-000001` 递增。客户、订单、支付、退款、会话和营销活动编码均使用固定宽度的补零数字后缀。

## 固定规模配置

创建 `data/generator/tiny.yaml`：

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

创建 `data/generator/full.yaml`：

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

派生行数是确定性的：

| 表 | Tiny | Full | 规则 |
|---|---:|---:|---|
| categories | 20 | 20 | 固定 |
| order_items | 约 6,300 | 约 630,000 | 每单 1–5 项，均值 2.1 |
| payments | 3,000 | 300,000 | 每个订单一次支付尝试 |
| refunds | 约 180 | 约 18,000 | 基础成功退款概率 6% |
| web_sessions | 15,000 | 1,200,000 | tiny 固定为订单数 5 倍，full 为 4 倍 |
| inventory_snapshots | 54,600 | 1,092,000 | 每个商品连续 546 天每天一行 |
| pipeline_runs | 1,638 | 1,638 | 连续 546 天每天三个流水线 |

近似行数会以实际值记录在 `DatasetManifest` 中；测试断言确定性相等，而不是断言近似值。

## 固定种子异常契约

| ID | 窗口 | 变异 | 预期信号 |
|---|---|---|---|
| `anomaly_gmv_drop_south_conversion` | 2026-06-08 至 2026-06-15 | 将 `SOUTH_REGIONS` 转化概率乘以 `0.65` | 与 2026-06-01 至 2026-06-08 相比，华南 GMV 和转化会话下降 |
| `anomaly_gmv_drop_stockout` | 2026-06-08 至 2026-06-15 | 将 `SKU-000001`、`SKU-000002` 可售量置零，并抑制其订单项 `0.90` | 两个商品对 GMV 损失有实质贡献 |
| `anomaly_refund_spike_category` | 2026-05-04 至 2026-05-11 | 将 `CAT-018` 的成功退款概率从 `0.06` 提高到 `0.24` | 品类退款率至少上升 2 倍 |
| `anomaly_inventory_delay` | 2026-06-15 | 省略 UTC 08:00 后的库存快照，并创建失败流水线运行 | 新鲜度规则发现陈旧 watermark |
| `anomaly_duplicate_order_items` | 2026-04-10 | 复制 20 条 tiny / 2,000 条 full 订单项，保留相同 `source_line_id` 并生成新主键 | 逻辑重复数量精确 |
| `anomaly_order_amount_mismatch` | 2026-03-17 | 为 10 个 tiny / 1,000 个 full 订单的 `orders.payable_amount` 增加人民币 10.00 元 | 订单—订单项对账失败 |
| `anomaly_missing_region` | 2026-02-12 | 将 10 个 tiny / 1,000 个 full 订单的 `orders.region` 置空 | 完整性规则发现精确空值数量 |
| `anomaly_refund_exceeds_payment` | 2026-05-20 | 使 3 条 tiny / 300 条 full 退款比成功支付额多人民币 50.00 元 | 退款一致性规则发现精确违规数 |

## 任务 1：生成器契约与命名随机流

**文件：**
- 修改：`pyproject.toml`
- 新建：`data/generator/tiny.yaml`
- 新建：`data/generator/full.yaml`
- 新建：`src/governed_analytics/data_generation/__init__.py`
- 新建：`src/governed_analytics/data_generation/models.py`
- 新建：`src/governed_analytics/data_generation/randomness.py`
- 新建：`tests/unit/data_generation/test_models.py`
- 新建：`tests/unit/data_generation/test_randomness.py`

**接口：**
- 输入：上述固定规模 YAML。
- 输出：`DatasetScale`、`GeneratorConfig`、`TableDigest`、`DatasetManifest`，以及 `named_rng(seed, namespace) -> numpy.random.Generator`。

- [x] **步骤 1：声明直接依赖和控制台入口**

在 `[project].dependencies` 中添加直接依赖 `numpy` 和 `pyyaml`；不得依赖传递安装。添加：

```toml
[project.scripts]
governed-data = "governed_analytics.data_generation.cli:main"
```

文件存在后运行 `uv lock`；将生成的 `uv.lock` 与任务 1 一并提交。

- [x] **步骤 2：编写失败的模型与 RNG 测试**

创建 `tests/unit/data_generation/test_randomness.py`：

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

创建 `tests/unit/data_generation/test_models.py`：

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

- [x] **步骤 3：运行测试并确认模块缺失**

运行：

```bash
uv run pytest tests/unit/data_generation/test_models.py tests/unit/data_generation/test_randomness.py -v
```

预期：由于数据生成模块不存在，导入失败。

- [x] **步骤 4：实现不可变契约**

创建 `models.py`，使用 `2026-09-01-week-1-data-baseline.md` 中精确的跨计划类。向 `GeneratorConfig` 添加以下验证器：

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

创建 `randomness.py`：

```python
from hashlib import sha256

import numpy as np
from numpy.random import Generator


def named_rng(seed: int, namespace: str) -> Generator:
    digest = sha256(f"{seed}:{namespace}".encode()).digest()
    namespace_seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return np.random.default_rng(namespace_seed)
```

- [x] **步骤 5：运行测试并提交契约**

运行：

```bash
uv lock
uv run pytest tests/unit/data_generation/test_models.py tests/unit/data_generation/test_randomness.py -v
```

预期：全部测试通过。

```bash
git add pyproject.toml uv.lock data/generator src/governed_analytics/data_generation tests/unit/data_generation
git commit -m "feat: 定义可复现数据生成契约"
```

## 任务 2：规范维表生成器

**文件：**
- 新建：`src/governed_analytics/data_generation/vocabulary.py`
- 新建：`src/governed_analytics/data_generation/dimensions.py`
- 新建：`tests/unit/data_generation/test_dimensions.py`

**接口：**
- 输入：`GeneratorConfig` 及命名流 `customers`、`products`、`campaigns`。
- 输出：`generate_categories()`、`generate_customers(config)`、`generate_products(config)` 和 `generate_campaigns(config)`；每个函数返回列顺序与 PostgreSQL Schema 一致的 DataFrame。

- [x] **步骤 1：编写确定性维表测试**

创建 `tests/unit/data_generation/test_dimensions.py`：

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

- [x] **步骤 2：运行测试并确认生成函数缺失**

运行：

```bash
uv run pytest tests/unit/data_generation/test_dimensions.py -v
```

预期：导入 `dimensions` 或 `load_generator_config` 失败。

- [x] **步骤 3：实现固定词汇与配置加载**

创建 `vocabulary.py`，包含本计划精确的地区、渠道元组及 20 个稳定的中文品类名。向 `models.py` 添加：

```python
from pathlib import Path

import yaml


def load_generator_config(path: str | Path) -> GeneratorConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GeneratorConfig.model_validate(raw)
```

- [x] **步骤 4：实现维表生成规则**

在 `dimensions.py` 中使用以下精确规则：

- categories：稳定 ID 1–20，编码 `CAT-001` 至 `CAT-020`；
- customers：ID 与编码顺序递增；地区按固定权重采样；分群权重为 `new=0.25`、`regular=0.60`、`vip=0.15`；注册时间在 `end_at` 前均匀分布；
- products：ID/SKU 顺序递增；品类均匀采样；标价按对数正态分布采样后限制在人民币 19.00–4,999.00 元；成本率采样范围 0.35–0.75；
- campaigns：ID/编码顺序递增；渠道从非自然渠道中选取；持续 14 天；开始日期互不重叠；花费人民币 5,000–100,000 元。

使用以下函数转换分：

```python
from decimal import Decimal


def cents_to_money(cents: int) -> str:
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")))
```

- [x] **步骤 5：运行测试、检查类型并提交维表**

运行：

```bash
uv run pytest tests/unit/data_generation/test_dimensions.py -v
uv run mypy src/governed_analytics/data_generation
```

预期：测试和类型检查通过。

```bash
git add src/governed_analytics/data_generation tests/unit/data_generation/test_dimensions.py
git commit -m "feat: 生成确定性业务维表"
```

## 任务 3：事实表生成器与业务季节性

**文件：**
- 新建：`src/governed_analytics/data_generation/facts.py`
- 新建：`tests/unit/data_generation/test_facts.py`

**接口：**
- 输入：已生成维表与命名随机流。
- 输出：异常注入前的基础 DataFrame，包括订单、订单项、支付、退款、会话、库存、归因及流水线运行。

- [x] **步骤 1：编写 tiny 事实契约测试**

创建 `tests/unit/data_generation/test_facts.py`：

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

- [x] **步骤 2：运行测试并确认事实模块缺失**

运行：

```bash
uv run pytest tests/unit/data_generation/test_facts.py -v
```

预期：导入 `facts` 失败。

- [x] **步骤 3：实现 `GeneratedFacts` 与时间戳分布**

创建不可变容器：

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

按以下权重生成订单时间戳：

- 12 月乘数 `1.25`；
- 6 月乘数 `0.92`；
- 周五至周日乘数 `1.15`；
- 其他日期乘数 `1.0`；
- 采样前归一化权重。

- [x] **步骤 4：实现精确的事实生成规则**

- 每个订单一次支付；成功概率：paid/completed/refunded 为 `0.97`，cancelled 为 `0.05`，placed 为 `0.30`；
- 支付对账前订单状态权重：placed `0.03`、paid `0.12`、completed `0.77`、cancelled `0.08`；
- 每单 1–5 个订单项，概率为 `[0.35, 0.35, 0.18, 0.08, 0.04]`；
- 订单项数量 1–3，概率为 `[0.78, 0.17, 0.05]`；
- 订单项折扣率从 `[0, 0.05, 0.10, 0.15]` 中采样，概率为 `[0.55, 0.20, 0.20, 0.05]`；
- 成功退款基础概率为 `0.06`；异常变异前，退款金额不得超过订单项净额；
- 会话：tiny 为 `orders * 5`，full 为 `orders * 4`；渠道权重为 `[0.30, 0.25, 0.20, 0.10, 0.15]`；已转化会话引用一个订单；
- 每个商品每天一条库存快照，基础数量 0–500，并加入补货噪声；
- 仅为营销活动窗口内的订单生成归因；每个营销活动/订单组合最多一行；
- 每天三个流水线记录：`orders`、`inventory` 和 `sessions`；通常状态为 succeeded，watermark 等于业务日结束时间。

返回前，确保每个 DataFrame 都按其 identity 列排序。

- [x] **步骤 5：运行测试并提交事实生成实现**

运行：

```bash
uv run pytest tests/unit/data_generation/test_facts.py -v
uv run ruff check src/governed_analytics/data_generation/facts.py
uv run mypy src/governed_analytics/data_generation/facts.py
```

预期：全部命令通过。

```bash
git add src/governed_analytics/data_generation/facts.py tests/unit/data_generation/test_facts.py
git commit -m "feat: 生成订单与行为事实数据"
```

## 任务 4：异常注入与真值清单

**文件：**
- 新建：`src/governed_analytics/data_generation/anomalies.py`
- 新建：`data/manifests/anomaly_manifest.schema.json`
- 新建：`tests/unit/data_generation/test_anomalies.py`

**接口：**
- 输入：`GeneratedFacts`、维表、规模及异常契约表。
- 输出：变异后的事实数据，以及序列化为 `anomaly_manifest.json` 的 `AnomalyManifest`。

- [x] **步骤 1：编写精确异常数量测试**

创建 `tests/unit/data_generation/test_anomalies.py`：

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

- [x] **步骤 2：运行测试并确认异常模块缺失**

运行：

```bash
uv run pytest tests/unit/data_generation/test_anomalies.py -v
```

预期：导入 `anomalies` 失败。

- [x] **步骤 3：定义异常模型与 JSON Schema**

实现：

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

使用 `AnomalyManifest.model_json_schema()` 生成并提交 `data/manifests/anomaly_manifest.schema.json`。测试必须比较已提交 Schema 与现场生成 Schema。

- [x] **步骤 4：实现全部八种确定性变异**

精确实现本计划开头的异常契约。应用时间/范围筛选后，按主键稳定升序选择行；绝不随机采样异常行。

对于 GMV 转化异常，确定性地将已转化的华南会话从 `true` 翻转为 `false`，清空其 `order_id`，将此前关联的订单设为 `cancelled`，并将其支付设为 `failed`、`paid_at` 置空。这样既保留订单记录，又将其排除在有效订单 GMV 之外。

对于缺货异常，删除异常窗口内受影响 SKU 订单项的 90%，并重新计算订单/支付合计。如果订单失去最后一个订单项，则保留订单，但将状态设为 `cancelled`、金额合计设为零，并将支付标记为失败。这些规则在保持配置订单数稳定的同时，使两个根因都能在 GMV 中观测到。

- [x] **步骤 5：运行测试并提交异常真值**

运行：

```bash
uv run pytest tests/unit/data_generation/test_anomalies.py -v
uv run mypy src/governed_analytics/data_generation/anomalies.py
```

预期：精确的 tiny 异常数量通过，清单 Schema 稳定。

```bash
git add src/governed_analytics/data_generation/anomalies.py data/manifests tests/unit/data_generation/test_anomalies.py
git commit -m "feat: 注入可验证业务与质量异常"
```

## 任务 5：规范 CSV 写入器、PostgreSQL 加载器与摘要

**文件：**
- 新建：`src/governed_analytics/data_generation/writer.py`
- 新建：`src/governed_analytics/data_generation/loader.py`
- 新建：`src/governed_analytics/data_generation/pipeline.py`
- 新建：`tests/unit/data_generation/test_writer.py`
- 新建：`tests/integration/data_generation/test_pipeline.py`

**接口：**
- 输入：已生成维表、变异后的事实数据，以及加载专用 `LoaderDatabaseSettings.loader_database_url`（`LOADER_DATABASE_URL`）。
- 输出：规范 CSV 文件、数据库行，以及包含逐表行数/摘要的 `DatasetManifest`。

- [x] **步骤 1：编写规范摘要测试**

创建 `tests/unit/data_generation/test_writer.py`：

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

- [x] **步骤 2：实现规范写入**

`write_canonical_csv` 必须：

```python
def write_canonical_csv(frame: pd.DataFrame, path: Path, *, sort_by: tuple[str, ...]) -> str:
    canonical = frame.sort_values(list(sort_by), kind="stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical.to_csv(path, index=False, lineterminator="\n", date_format="%Y-%m-%dT%H:%M:%SZ")
    return sha256(path.read_bytes()).hexdigest()
```

- [x] **步骤 3：实现静态 COPY 注册表与加载器**

仅在加载器边界内实例化 `LoaderDatabaseSettings`；不得在其中实例化 `DatabaseSettings` 或 `MigrationDatabaseSettings`。加载器对象不得包含只读 URL 或迁移 URL。

在 `loader.py` 中定义 `TABLE_LOAD_ORDER` 及精确 CSV 列元组。只接受该注册表中的名称。按以下顺序加载：

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

使用 psycopg COPY：

```python
with connection.cursor().copy(
    f"copy {table_name} ({', '.join(columns)}) from stdin with (format csv, header true)"
) as copy:
    with csv_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            copy.write(chunk)
```

该 f-string 只有在 `table_name` 与 `columns` 来自静态注册表、绝不来自 CLI 输入时才安全。

- [x] **步骤 4：编写并通过 tiny 流水线集成测试**

创建 `tests/integration/data_generation/test_pipeline.py`：

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

第二次加载前，`generate_and_load` 使用加载器连接只清空 12 张已知表，并重置 identity。它必须拒绝用户输入的数据库名或表名。

- [x] **步骤 5：运行测试并提交加载流水线**

运行：

```bash
uv run pytest tests/unit/data_generation/test_writer.py -v
uv run pytest tests/integration/data_generation/test_pipeline.py -v
```

预期：规范摘要测试与两次加载的数据库可复现性测试通过。

```bash
git add src/governed_analytics/data_generation tests/unit/data_generation tests/integration/data_generation
git commit -m "feat: 加载并校验可复现模拟数据"
```

## 任务 6：首批 15 个指标定义

**文件：**
- 新建：`src/governed_analytics/domain/metrics.py`
- 新建：`src/governed_analytics/metrics/__init__.py`
- 新建：`src/governed_analytics/metrics/catalog.py`
- 新建：`data/metrics/core.yaml`
- 新建：`tests/unit/metrics/test_catalog.py`
- 新建：`tests/integration/metrics/test_metric_queries.py`

**接口：**
- 输入：已加载的数据库 Schema 与固定业务定义。
- 输出：`MetricDefinition`、`load_metric_catalog(path)`、`get_metric(metric_id)`，以及 15 个已解析定义。

- [x] **步骤 1：编写目录验证测试**

创建 `tests/unit/metrics/test_catalog.py`：

```python
from governed_analytics.metrics.catalog import load_metric_catalog


def test_core_catalog_has_exact_metric_ids_and_valid_sql() -> None:
    catalog = load_metric_catalog("data/metrics/core.yaml")

    assert tuple(catalog) == (
        "gmv", "paid_gmv", "net_revenue", "valid_order_count", "average_order_value",
        "payment_success_rate", "refund_amount", "refund_rate", "active_customers",
        "new_customers", "repeat_purchase_rate", "customer_acquisition_cost", "conversion_rate",
        "stockout_rate", "campaign_roi",
    )
```

加载器必须拒绝重复 ID、未知来源表、不含时区的 `valid_from`，以及 SQLGlot 无法在 `select <expression>` 中解析的表达式。

- [x] **步骤 2：在 YAML 中定义精确指标语义**

创建 `data/metrics/core.yaml`，版本为 `1.0.0`、`valid_from: 2025-01-01T00:00:00Z`，并使用以下公式：

| 指标 ID | 表达式 | 时间字段 | 单位 | 允许维度 |
|---|---|---|---|---|
| `gmv` | 订单状态为 `paid, completed, refunded` 时的 `sum(oi.net_amount)` | `o.ordered_at` | CNY | region、channel、category、product、segment |
| `paid_gmv` | 支付成功时的 `sum(p.amount)` | `p.paid_at` | CNY | region、channel、segment |
| `net_revenue` | 成功支付金额减去成功退款金额 | `o.ordered_at` | CNY | region、channel、category、product |
| `valid_order_count` | 状态为 `paid, completed, refunded` 的去重订单数 | `o.ordered_at` | count | region、channel、segment |
| `average_order_value` | `gmv / nullif(valid_order_count, 0)` | `o.ordered_at` | CNY | region, channel, segment |
| `payment_success_rate` | 成功支付尝试数 / 全部支付尝试数 | `p.created_at` | ratio | provider、channel |
| `refund_amount` | 成功退款金额 | `r.refunded_at` | CNY | reason、category、product、region |
| `refund_rate` | 成功退款金额 / 匹配订单的成功支付金额 | `o.ordered_at` | ratio | category、product、region |
| `active_customers` | 拥有有效订单的去重客户数 | `o.ordered_at` | count | region、segment |
| `new_customers` | 区间内注册客户数 | `c.registered_at` | count | region、segment |
| `repeat_purchase_rate` | 截至区间末至少有两个有效历史订单的客户数 / 活跃客户数 | `o.ordered_at` | ratio | region、segment |
| `customer_acquisition_cost` | 按营销活动预聚合的花费 / 区间内去重归因客户数 | `a.attributed_at` | CNY | campaign、channel |
| `conversion_rate` | 已转化会话数 / 全部会话数 | `s.occurred_at` | ratio | channel、region |
| `stockout_rate` | 可售量为零的商品快照数 / 全部商品快照数 | `i.snapshot_at` | ratio | category、product |
| `campaign_roi` | `(attributed_revenue - campaign_spend) / nullif(campaign_spend, 0)` | `a.attributed_at` | ratio | campaign, channel |

每个 YAML 条目包含说明、默认筛选条件、来源表清单及一个示例问题。比率以 0–1 存储，而不是百分数。

- [x] **步骤 3：实现不可变指标契约与加载器**

实现主计划中的精确 `MetricDefinition`。`load_metric_catalog` 返回保持文件顺序的 `dict[str, MetricDefinition]`。使用以下方式验证表达式：

```python
sqlglot.parse_one(f"select {metric.expression_sql}", read="postgres")
```

目录加载器只验证元数据；不会把用户输入拼接到可执行 SQL 中。

- [x] **步骤 4：为全部 15 个指标添加数据库健全性查询**

创建集成测试，在 `[2026-06-01, 2026-07-01)` 区间内为每个指标执行一条固定的参数化查询。断言：

- 金额与计数非负；
- 分母为零时比率为空，否则位于 `[0, 1]`；`campaign_roi` 例外，它可以为负；
- 每条查询都使用 `analytics_readonly`，并在 `statement_timeout = '10s'` 下完成。

每条固定指标查询都必须在显式事务中执行；engine 由只读专用 `DatabaseSettings.database_url` 创建。查询前，在同一连接中按以下顺序执行语句：

```sql
set transaction read only;
set local statement_timeout = '10s';
set local search_path = public, pg_catalog;
```

真实 PostgreSQL 集成测试（非 mock）必须在同一事务中查询 `current_setting`，并断言 `transaction_read_only = 'on'`、`statement_timeout = '10s'` 和 `search_path = 'public, pg_catalog'`。不得在通用 engine 工厂上设置事务状态。

- [x] **步骤 5：运行测试并提交指标目录**

运行：

```bash
uv run pytest tests/unit/metrics -v
uv run pytest tests/integration/metrics -v
```

预期：15 个定义成功解析，全部固定验证查询完成。

```bash
git add data/metrics src/governed_analytics/domain/metrics.py src/governed_analytics/metrics tests/unit/metrics tests/integration/metrics
git commit -m "feat: 定义首批十五个业务指标"
```

## 任务 7：CLI、CI 与数据文档

**文件：**
- 新建：`src/governed_analytics/data_generation/cli.py`
- 修改：`Makefile`
- 修改：`.github/workflows/ci.yml`
- 新建：`docs/data-generation.md`
- 新建：`docs/metrics.md`
- 修改：`README.md`

**接口：**
- 输入：生成流水线与指标目录。
- 输出：`governed-data generate`、`governed-data verify`、`make data-tiny` 和 `make metrics-check`。

- [x] **步骤 1：实现具有显式规模选项的 CLI 命令**

使用 `argparse` 提供以下接口：

```text
governed-data generate --scale {tiny,full} [--output artifacts/datasets]
governed-data verify --scale {tiny,full} [--output artifacts/datasets]
```

`generate` 加载数据并写入清单。`verify` 在临时产物目录中重新生成，比较配置/表摘要，打印存在差异的表名，并在不匹配时以状态码 1 退出。

- [x] **步骤 2：添加 Make target**

```make
.PHONY: data-tiny data-verify metrics-check

data-tiny:
	@uv run governed-data generate --scale tiny

data-verify:
	@uv run governed-data verify --scale tiny

metrics-check:
	@uv run pytest tests/unit/metrics tests/integration/metrics -v
```

- [x] **步骤 3：扩展 CI，但只允许 tiny 生成**

在 database job 的迁移与持久化测试之后添加：

```yaml
      - run: uv run governed-data generate --scale tiny
      - run: uv run governed-data verify --scale tiny
      - run: uv run pytest tests/unit/metrics tests/integration/metrics -v
```

CI 绝不能生成 full 数据集。

- [x] **步骤 4：记录可复现性与指标语义**

`docs/data-generation.md` 记录各规模行数、种子、命名 RNG、全部八种异常、产物、清单及安全重生成。`docs/metrics.md` 记录 15 个指标 ID、公式、时间字段、维度及比率表示方式。

- [x] **步骤 5：运行完整 Plan B 门禁**

运行：

```bash
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check
make check
git diff --check
```

预期：生成与验证完成且摘要一致；15 个指标测试与质量检查通过。

- [x] **步骤 6：提交 Plan B 工作流**

```bash
git add src/governed_analytics/data_generation/cli.py Makefile .github/workflows/ci.yml docs/data-generation.md docs/metrics.md README.md
git commit -m "docs: 固化数据与指标复现流程"
```

## Plan B 完成门禁

- [x] tiny 生成在开发机上 60 秒内完成（2026-09-04 实测 2.73 秒）。
- [x] full 生成在 15 分钟内完成，并使用分块 COPY，而非逐行 INSERT（2026-09-04 完整 verify 约 98 秒）。
- [x] 使用种子 `20260901` 的两次运行具有相同的逐表行数与 SHA-256 摘要。
- [x] 八条异常记录与精确的 tiny 变异数量及预期信号一致。
- [x] 异常注入后全部外键仍然有效。
- [x] 15 个指标定义均可解析、验证，并执行固定健全性查询。
- [x] Git 未暂存任何生成 CSV 或数据库卷。
- [ ] CI 执行 tiny 生成，且不发起外部 API 请求（工作流静态门禁已通过，外部运行待触发）。
