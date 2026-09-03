# 第 1 周：数据基础与基线实现计划

> **状态同步（2026-09-04）：** Plan A、Plan B、Plan C、本地离线门禁与 DeepSeek v1/v2 live 均已完成，
> 两轮 live 证据已版本化归档。v2 为 5/20、有效 SQL 14/20；外部 CI 尚无运行记录，不阻塞本地进入第 2 周。

> **供 Agent 执行者使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务实施本计划。各步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 在开始任何 LangGraph Agent 实现之前，建立可复现的电商数据真值层，以及可度量的直接 Text-to-SQL 基线。

**架构：** 第 1 周拆分为三个按顺序执行、可独立审查的计划。Plan A 创建 PostgreSQL、Schema、迁移、角色与 CI；Plan B 创建确定性合成数据、固定种子异常及首批 15 个指标定义；Plan C 创建 20 个黄金问题、只读的直接 Text-to-SQL 基线、评分与报告。

**技术栈：** Python 3.12、uv、PostgreSQL 17、pgvector 0.8.6、Docker Compose、SQLAlchemy 2、Alembic、psycopg 3、Pydantic 2、NumPy/Pandas、SQLGlot、OpenAI Python SDK 3.6、Pytest、Ruff、mypy、GitHub Actions。

**规格文档：** `GOVERNED_ANALYTICS_AGENT_PLAN.md`

## 全局约束

- 使用 Python `3.12`；保持 `requires-python = ">=3.12,<3.13"` 不变。
- 使用 `uv`；依赖变化时同时提交 `pyproject.toml` 与 `uv.lock`。
- 使用 `pgvector/pgvector:0.8.6-pg17-bookworm`；不得使用浮动的 `latest` 标签。
- 所有时间戳使用 UTC `timestamptz`，金额使用 `numeric(14,2)`，标识符使用小写 `snake_case`，单数据库主键使用 `bigint identity`。
- 为每个外键建立索引；仅针对已声明的筛选/连接模式添加复合索引。
- 测试和默认命令不得调用付费模型。真实调用必须同时提供 `MODEL_API_KEY` 和显式 `--live` 标志。
- 不得提交 `.env`、数据库卷、生成的 full 数据集、包含秘密的原始模型响应或 API Key。
- 基线数据库账号只读；生成的 SQL 也必须在只读事务中运行，并设置 10 秒语句超时。
- 指标查询、Oracle 物化和基线 SQL 执行还必须把事务局部 `search_path` 设置为 `public, pg_catalog`；各集成测试套件都要在 PostgreSQL 上验证三层运行时防线：`transaction_read_only`、`statement_timeout` 与 `search_path`。
- 固定种子：`20260901`。业务数据区间：`[2025-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`。
- tiny 规模：500 个客户、100 个商品、3,000 个订单。full 规模：50,000 个客户、2,000 个商品、300,000 个订单。
- 第 1 周不实现 LangGraph 图、MCP Server、RAG/向量检索、Streamlit UI、审批流或任意 Python 执行。
- 每个任务遵循红—绿—重构，并以一个聚焦的中文 Git 提交结束。

---

## 当前仓库基线

- `main` 跟踪私有远程分支 `origin/main`。
- Docker Desktop、Docker Compose、Python 3.12、uv、lint、类型检查和 Pytest 均可用。
- `src/governed_analytics/__init__.py` 是当前唯一的包代码。
- 尚无 `docker-compose.yml`、数据库 Schema、迁移、合成数据集、指标目录、黄金用例或基线执行器。
- 模型 Key 有意留空。Plan A、B 无需 Key 即可完整执行；Plan C 的全部测试使用 fixture。

## 计划拆分

| 顺序 | 计划 | 可独立测试的产物 | 依赖 |
|---:|---|---|---|
| 1 | [Plan A：PostgreSQL Schema](2026-09-01-week-1a-postgres-schema.md) | 健康数据库、12 表 Schema、已索引外键、加载/只读角色、迁移测试 | 仓库基线 |
| 2 | [Plan B：合成数据与指标](2026-09-01-week-1b-synthetic-data-metrics.md) | 确定性的 tiny/full 生成器、异常清单、首批 15 个已验证指标 | Plan A |
| 3 | [Plan C：黄金基线](2026-09-01-week-1c-golden-baseline.md) | 20 个 Oracle 支撑的用例、fixture/live Text-to-SQL 执行器、评分与成本报告 | Plan A、B |

前一计划的门禁未通过、提交未进入 `main` 前，不得开始后一计划。

## 跨计划契约

以下公共名称在三个计划中固定不变：

```python
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DatasetScale(StrEnum):
    TINY = "tiny"
    FULL = "full"


class GeneratorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    scale: DatasetScale
    seed: int = 20260901
    start_at: datetime
    end_at: datetime
    customers: int = Field(gt=0)
    products: int = Field(gt=0)
    orders: int = Field(gt=0)
    campaigns: int = Field(gt=0)


class TableDigest(BaseModel):
    table_name: str
    row_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DatasetManifest(BaseModel):
    dataset_id: str
    config_sha256: str
    seed: int
    scale: DatasetScale
    tables: tuple[TableDigest, ...]
    anomaly_manifest_path: Path


class MetricDefinition(BaseModel):
    metric_id: str
    name_zh: str
    name_en: str
    description: str
    expression_sql: str
    time_field: str
    default_filters: tuple[str, ...]
    dimensions: tuple[str, ...]
    source_tables: tuple[str, ...]
    unit: Literal["cny", "count", "ratio"]
    version: str
    valid_from: datetime


class GoldenCase(BaseModel):
    case_id: str
    question: str
    category: str
    oracle_sql_path: Path
    comparison: Literal["scalar", "table", "top_k", "boolean"]
    key_columns: tuple[str, ...] = ()
    numeric_columns: tuple[str, ...] = ()
    absolute_tolerance: Decimal = Decimal("0.01")
    relative_tolerance: Decimal = Decimal("0.000001")


class BaselineCaseResult(BaseModel):
    case_id: str
    generated_sql: str | None
    status: Literal["passed", "wrong_answer", "invalid_sql", "execution_error"]
    score: Decimal = Field(ge=0, le=1)
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_cny: Decimal = Field(ge=0)
    error_type: str | None = None
```

实现后，规范定义分别位于以下文件：

| 契约 | 规范文件 |
|---|---|
| 只读专用 `DatabaseSettings`、加载专用 `LoaderDatabaseSettings`、迁移专用 `MigrationDatabaseSettings` | `src/governed_analytics/config.py` |
| `DatasetScale`, `GeneratorConfig`, `TableDigest`, `DatasetManifest` | `src/governed_analytics/data_generation/models.py` |
| `MetricDefinition` | `src/governed_analytics/domain/metrics.py` |
| `GoldenCase`, `BaselineCaseResult` | `src/governed_analytics/evals/models.py` |

数据库设置被刻意设计为按角色隔离的秘密容器。`DatabaseSettings` 只声明 `database_url`，`LoaderDatabaseSettings` 只声明 `loader_database_url`，`MigrationDatabaseSettings` 只声明 `migration_database_url`；不允许提供合并式兼容设置对象。按 URL 语义进行百分号解码后，每个类都验证精确 driver、显式非空密码及其规范角色（`analytics_readonly`、`analytics_loader` 或 `governed_admin`），同时拒绝编码后等价的用户名写法。

## 执行顺序

- [x] **步骤 1：执行 Plan A，并通过数据库门禁**

运行：

```bash
uv run pytest tests/unit tests/integration/persistence -v
docker compose config --quiet
uv run alembic current
```

预期：全部测试通过、Compose 配置有效，且 Alembic 报告版本 `0003 (head)`。

- [x] **步骤 2：执行 Plan B，并通过可复现性门禁**

运行：

```bash
uv run governed-data generate --scale tiny
uv run governed-data verify --scale tiny
uv run pytest tests/unit/data_generation tests/integration/data_generation -v
```

预期：两次 tiny 生成得到相同的行数和逐表摘要；15 个指标均可在已加载数据库上通过验证。

- [x] **步骤 3：以 fixture 模式执行 Plan C**

运行：

```bash
uv run governed-eval baseline --dataset tiny --mode fixture
uv run pytest tests/unit/evals tests/integration/evals -v
```

预期：20 个用例全部执行；fixture 模式不发起网络请求；JSON 与 Markdown 报告均生成在 `artifacts/evals/baseline/fixture/` 下。

- [x] **步骤 4：仅在用户提供 Key 后，以 live 模式执行 Plan C**

运行：

```bash
uv run governed-eval baseline --dataset tiny --mode live --live
```

预期：20 个用例生成一份包含结果准确率、有效 SQL 率、执行成功率、延迟、Token 用量、预估人民币成本及分类失败的报告。

- [x] **步骤 5：执行完整的第 1 周门禁**

运行：

```bash
make doctor
make check
make db-up
make migrate
make data-tiny
make metrics-check
make eval-fixture
git diff --check
```

预期：

- 环境检查无错误；
- lint、mypy、单元测试和集成测试通过；
- PostgreSQL 健康，全部迁移位于 head；
- tiny 数据集可复现；
- 15 个指标定义通过验证；
- 20 个 fixture 基线用例在无网络访问的情况下完成；
- Git 不存在空白字符错误。

## 第 1 周完成定义

- [x] PostgreSQL 17 + pgvector 可通过一条 Docker Compose 命令启动。
- [x] 12 张业务表全部使用显式类型、约束及已索引的外键。
- [x] 加载角色和只读角色均由集成测试证明；只读角色不能执行插入、更新、删除、创建或修改。
- [x] tiny 与 full 生成器配置固定且已版本化。
- [x] 使用相同配置和种子重复运行时，逐表摘要完全相同。
- [x] `anomaly_manifest.json` 记录异常 ID、时间窗口、影响范围、根因与预期信号。
- [x] 首批 15 个指标具有版本化 YAML 定义和可执行验证查询。
- [x] 20 个黄金问题具有版本化 Oracle SQL，以及从 tiny 数据集生成的预期输出。
- [x] fixture 基线确定且无网络访问。
- [x] live 基线获授权后记录失败，不删除困难用例。
- [x] GitHub Actions 在不调用付费 API 的情况下执行 lint、类型检查、单元测试、数据库集成测试、迁移、tiny 生成及 fixture 评测。
- [x] README 包含准确的第 1 周环境搭建、生成、验证与基线命令。

## 明确延后

- LangGraph 编排与 AgentState：第 3 周。
- 完整 SQL 策略引擎和攻击测试集：第 2 周；第 1 周仅使用窄范围基线守卫和数据库只读权限。
- FastAPI/SSE 与 Streamlit：第 3、4 周。
- Checkpoint、审批、数据质量编排及失败恢复：第 5 周。
- pgvector 检索内容：后续 RAG 工作；第 1 周只验证扩展可安装。
- MCP 与 OpenTelemetry 导出：第 7 周。

## 易变接口所用来源

- pgvector Docker 标签：`https://hub.docker.com/r/pgvector/pgvector/tags`
- pgvector 仓库：`https://github.com/pgvector/pgvector`
- OpenAI-compatible Chat Completions 接口：`https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions`

## 自审记录

| 第 1 周规格要求 | 实现位置 |
|---|---|
| 初始化 Python 项目与 CI | 现有脚手架 + Plan A Task 6 |
| PostgreSQL Schema 与迁移 | Plan A Tasks 1–5 |
| 固定种子的 tiny/full 数据 | Plan B Tasks 1–5 |
| 机器可读的异常真值 | Plan B Task 4 |
| 首批 15 个指标 | Plan B Task 6 |
| 20 个黄金问题与 Oracle 答案 | Plan C Tasks 1–2 |
| 直接 Text-to-SQL 基线 | Plan C Tasks 3–7 |
| 准确率、成本、延迟与失败报告 | Plan C Tasks 5–7 |
| Docker、测试、可复现性与 CI 门禁 | 三个计划的完成门禁 |

自审结论：

- 项目规格中的所有第 1 周任务和门禁都映射到具体计划任务；
- 不存在未解决标记、占位说明或无人负责的接口；
- 规范类型只在主契约中声明一次，并分配到明确实现文件；
- 测试与 CI 无法发起真实模型调用；
- API Key 已在被 Git 忽略的本地 `.env` 配置，live 仍只允许显式双重授权，不影响离线评测；
- 第 2 周及以后能力均明确延后，避免第 1 周过早膨胀为 Agent 实现。
