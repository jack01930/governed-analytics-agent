# Week 1 Data Foundation and Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible ecommerce data truth layer and a measured direct Text-to-SQL baseline before any LangGraph agent implementation begins.

**Architecture:** Week 1 is split into three sequential, independently reviewable plans. Plan A creates PostgreSQL, schema, migrations, roles, and CI; Plan B creates deterministic synthetic data, seeded anomalies, and the first 15 metric definitions; Plan C creates 20 golden questions, a read-only direct Text-to-SQL baseline, scoring, and reports.

**Tech Stack:** Python 3.12, uv, PostgreSQL 17, pgvector 0.8.6, Docker Compose, SQLAlchemy 2, Alembic, psycopg 3, Pydantic 2, NumPy/Pandas, SQLGlot, OpenAI Python SDK 3.6, Pytest, Ruff, mypy, GitHub Actions.

**Spec:** `GOVERNED_ANALYTICS_AGENT_PLAN.md`

## Global Constraints

- Use Python `3.12`; keep `requires-python = ">=3.12,<3.13"` unchanged.
- Use `uv` and commit both `pyproject.toml` and `uv.lock` whenever dependencies change.
- Use `pgvector/pgvector:0.8.6-pg17-bookworm`; do not use floating `latest` tags.
- Store all timestamps as UTC `timestamptz`, money as `numeric(14,2)`, identifiers as lowercase `snake_case`, and single-database primary keys as `bigint identity`.
- Index every foreign key and add composite indexes only for declared filter/join patterns.
- Never let tests or default commands call a paid model. Live calls require both `MODEL_API_KEY` and an explicit `--live` flag.
- Never commit `.env`, database volumes, generated full datasets, raw model responses containing secrets, or API keys.
- The baseline database login is read-only; generated SQL also runs inside a read-only transaction with a 10-second statement timeout.
- Metric queries, Oracle materialization, and baseline SQL execution also set the transaction-local
  search path to `public, pg_catalog`; each integration suite asserts all three runtime defenses
  (`transaction_read_only`, `statement_timeout`, and `search_path`) against PostgreSQL.
- Fixed seed: `20260901`. Business data range: `[2025-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`.
- Tiny scale: 500 customers, 100 products, 3,000 orders. Full scale: 50,000 customers, 2,000 products, 300,000 orders.
- Week 1 implements no LangGraph graph, MCP server, RAG/vector retrieval, Streamlit UI, approval flow, or arbitrary Python execution.
- Each task follows red-green-refactor and ends in one focused Chinese Git commit.

---

## Current Repository Baseline

- `main` tracks private `origin/main`.
- Docker Desktop, Docker Compose, Python 3.12, uv, linting, typing, and Pytest are operational.
- `src/governed_analytics/__init__.py` is the only package code.
- There is no `docker-compose.yml`, database schema, migration, synthetic dataset, metric catalog, golden case, or baseline runner yet.
- The model key is intentionally absent. Plans A and B are fully executable without it; all Plan C tests use fixtures.

## Plan Decomposition

| Order | Plan | Independently testable output | Depends on |
|---:|---|---|---|
| 1 | [Plan A: PostgreSQL Schema](2026-09-01-week-1a-postgres-schema.md) | Healthy database, 12-table schema, indexed foreign keys, loader/read-only roles, migration tests | Repository baseline |
| 2 | [Plan B: Synthetic Data and Metrics](2026-09-01-week-1b-synthetic-data-metrics.md) | Deterministic tiny/full generator, anomaly manifest, first 15 validated metrics | Plan A |
| 3 | [Plan C: Golden Baseline](2026-09-01-week-1c-golden-baseline.md) | 20 oracle-backed cases, fixture/live Text-to-SQL runner, score and cost report | Plans A and B |

Do not start a later plan until the preceding plan's gate is green and its commit is on `main`.

## Cross-Plan Contracts

The following public names are fixed across all three plans:

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

The canonical definitions live in these files when implemented:

| Contract | Canonical file |
|---|---|
| readonly-only `DatabaseSettings`, loader-only `LoaderDatabaseSettings`, migration-only `MigrationDatabaseSettings` | `src/governed_analytics/config.py` |
| `DatasetScale`, `GeneratorConfig`, `TableDigest`, `DatasetManifest` | `src/governed_analytics/data_generation/models.py` |
| `MetricDefinition` | `src/governed_analytics/domain/metrics.py` |
| `GoldenCase`, `BaselineCaseResult` | `src/governed_analytics/evals/models.py` |

Database settings are intentionally role-scoped secret containers. `DatabaseSettings` declares
only `database_url`, `LoaderDatabaseSettings` only `loader_database_url`, and
`MigrationDatabaseSettings` only `migration_database_url`; no combined compatibility settings
object is allowed. After URL-semantic percent-decoding, each class validates an exact driver,
explicit nonempty password, and its canonical role (`analytics_readonly`, `analytics_loader`, or
`governed_admin`), while encoded-equivalent username spellings are rejected.

## Execution Order

- [ ] **Step 1: Execute Plan A and pass its database gate**

Run:

```bash
uv run pytest tests/unit tests/integration/persistence -v
docker compose config --quiet
uv run alembic current
```

Expected: all tests pass, Compose config is valid, and Alembic reports revision `0002 (head)`.

- [ ] **Step 2: Execute Plan B and pass its reproducibility gate**

Run:

```bash
uv run governed-data generate --scale tiny
uv run governed-data verify --scale tiny
uv run pytest tests/unit/data_generation tests/integration/data_generation -v
```

Expected: two tiny generations produce identical row counts and per-table digests; 15 metrics validate against the loaded database.

- [ ] **Step 3: Execute Plan C in fixture mode**

Run:

```bash
uv run governed-eval baseline --dataset tiny --mode fixture
uv run pytest tests/unit/evals tests/integration/evals -v
```

Expected: 20 cases execute, fixture mode makes zero network calls, and both JSON and Markdown reports are created under `artifacts/evals/baseline/fixture/`.

- [ ] **Step 4: Execute Plan C in live mode only after the user supplies a key**

Run:

```bash
uv run governed-eval baseline --dataset tiny --mode live --live
```

Expected: 20 cases produce a report with result accuracy, valid SQL rate, execution success, latency, token usage, estimated CNY cost, and categorized failures.

- [ ] **Step 5: Run the complete Week 1 gate**

Run:

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

Expected:

- environment doctor has zero errors;
- lint, mypy, unit tests, and integration tests pass;
- PostgreSQL is healthy and all migrations are at head;
- the tiny dataset is reproducible;
- 15 metric definitions validate;
- 20 fixture baseline cases finish without network access;
- Git has no whitespace errors.

## Week 1 Definition of Done

- [ ] PostgreSQL 17 + pgvector starts from one Docker Compose command.
- [ ] All 12 business tables use explicit types, constraints, and indexed foreign keys.
- [ ] Loader and read-only roles are proven by integration tests; the read-only role cannot insert, update, delete, create, or alter.
- [ ] Tiny and full generator configs are fixed and versioned.
- [ ] Re-running the same config and seed produces identical table digests.
- [ ] `anomaly_manifest.json` records anomaly ID, time window, affected scope, root cause, and expected signal.
- [ ] The first 15 metrics have versioned YAML definitions and executable validation queries.
- [ ] Twenty golden questions have versioned oracle SQL and expected outputs generated from the tiny dataset.
- [ ] Fixture baseline is deterministic and network-free.
- [ ] Live baseline, when authorized, records failures instead of deleting difficult cases.
- [ ] GitHub Actions runs lint, type checks, unit tests, database integration tests, migrations, tiny generation, and fixture evaluation without a paid API call.
- [ ] README contains exact Week 1 setup, generation, verification, and baseline commands.

## Explicitly Deferred

- LangGraph orchestration and AgentState: Week 3.
- Full SQL policy engine and attack suite: Week 2; Week 1 uses a narrow baseline guard plus database read-only permissions.
- FastAPI/SSE and Streamlit: Weeks 3 and 4.
- Checkpoint, approval, data-quality orchestration, and failure recovery: Week 5.
- pgvector retrieval content: later RAG work; Week 1 only verifies that the extension can be installed.
- MCP and OpenTelemetry export: Week 7.

## Sources Used for Volatile Interfaces

- pgvector Docker tags: `https://hub.docker.com/r/pgvector/pgvector/tags`
- pgvector repository: `https://github.com/pgvector/pgvector`
- OpenAI-compatible Chat Completions surface: `https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions`

## Self-Review Record

| Week 1 specification requirement | Implemented by |
|---|---|
| Initialize Python project and CI | Existing scaffold plus Plan A Task 6 |
| PostgreSQL Schema and migrations | Plan A Tasks 1-5 |
| Fixed-seed tiny/full data | Plan B Tasks 1-5 |
| Machine-readable anomaly truth | Plan B Task 4 |
| First fifteen metrics | Plan B Task 6 |
| Twenty golden questions and Oracle answers | Plan C Tasks 1-2 |
| Direct Text-to-SQL baseline | Plan C Tasks 3-7 |
| Accuracy, cost, latency, and failure report | Plan C Tasks 5-7 |
| Docker, tests, reproducibility, and CI gates | All three plan completion gates |

Self-review result:

- all Week 1 tasks and gates from the project specification map to a plan task;
- no unresolved marker, placeholder instruction, or unowned interface remains;
- canonical types are declared once in the master contract and assigned to exact implementation files;
- tests and CI cannot make live model calls;
- API key absence blocks only the explicitly authorized live baseline run, not Plans A/B or fixture evaluation;
- Week 2+ capabilities are explicitly deferred so Week 1 does not become a premature Agent implementation.
