# Week 1A PostgreSQL Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Dockerized PostgreSQL 17 database with pgvector, twelve ecommerce tables, deterministic Alembic migrations, least-privilege loader/read-only roles, and executable schema contract tests.

**Architecture:** Docker Compose owns the local database lifecycle. Alembic migrations, executed by an admin-only migration connection, are the sole schema source of truth. Application code uses async SQLAlchemy connections; bulk loading uses the dedicated loader role; analytics and future Agent queries use a separately tested read-only role.

**Tech Stack:** Docker Compose, `pgvector/pgvector:0.8.6-pg17-bookworm`, PostgreSQL 17, SQLAlchemy 2, Alembic 1.19, asyncpg, psycopg 3, Pydantic Settings, Pytest.

**Spec:** `GOVERNED_ANALYTICS_AGENT_PLAN.md` sections 18-21, 26, 38-39, and `docs/superpowers/plans/2026-09-01-week-1-data-baseline.md`.

## Global Constraints

- Use lowercase `snake_case` identifiers without quoted mixed-case names.
- Use `bigint generated always as identity` primary keys.
- Use `text` for strings, `timestamptz` for instants, `date` for business dates, `numeric(14,2)` for CNY, and `boolean` for flags.
- Index every foreign key. Composite indexes place equality columns before range columns.
- Use database constraints for structural validity, but do not add cross-table constraints that would prevent seeded data-quality anomalies.
- `analytics_readonly` receives only `CONNECT`, schema `USAGE`, and table `SELECT`.
- `analytics_loader` receives table DML, `TRUNCATE`, and sequence usage for deterministic local dataset rebuilds, but no DDL privileges.
- The admin URL is used only by Alembic and local setup commands.
- Tests may recreate an ephemeral CI database; no command in this plan removes the developer's named Docker volume by default.

---

## Schema Contract

| Table | Primary key | Required foreign keys | Intentional anomaly allowance |
|---|---|---|---|
| `customers` | `customer_id bigint identity` | none | none |
| `categories` | `category_id bigint identity` | none | none |
| `products` | `product_id bigint identity` | `category_id -> categories` | none |
| `orders` | `order_id bigint identity` | `customer_id -> customers` | `region` may be null for completeness cases |
| `order_items` | `order_item_id bigint identity` | `order_id -> orders`, `product_id -> products` | `source_line_id` is not unique so logical duplicates can be inserted |
| `payments` | `payment_id bigint identity` | `order_id -> orders` | totals need not match `orders` for consistency cases |
| `refunds` | `refund_id bigint identity` | `order_id -> orders`, nullable `order_item_id -> order_items` | refund may exceed successful payment for quality cases |
| `inventory_snapshots` | `inventory_snapshot_id bigint identity` | `product_id -> products` | freshness failure is represented by missing recent snapshots |
| `web_sessions` | `session_id bigint identity` | nullable `customer_id -> customers`, nullable `order_id -> orders` | anonymous sessions are valid |
| `marketing_campaigns` | `campaign_id bigint identity` | none | none |
| `campaign_attributions` | `attribution_id bigint identity` | `campaign_id -> marketing_campaigns`, `order_id -> orders` | none |
| `pipeline_runs` | `pipeline_run_id bigint identity` | none | failed runs may have null `finished_at` and stale watermark |

Deletion policy is `restrict` for business facts. Synthetic datasets are rebuilt as a whole; application code must not cascade-delete analytical history.

## Task 1: Database Settings and Compose Service

**Files:**
- Create: `src/governed_analytics/config.py`
- Create: `tests/unit/test_config.py`
- Create: `docker-compose.yml`
- Create: `infra/docker/postgres/init/001_bootstrap.sql`
- Modify: `.env.example`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: environment variables documented in `.env.example`.
- Produces: `DatabaseSettings`, a healthy Compose service named `db`, and roles `analytics_loader` and `analytics_readonly`.

- [ ] **Step 1: Register Pytest markers and write failing settings tests**

Add to `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
markers = [
  "integration: requires local Docker services",
  "live_model: requires explicit paid model authorization",
]
```

Create `tests/unit/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from governed_analytics.config import DatabaseSettings


def test_database_settings_accept_three_separate_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://readonly:pw@db:5432/app")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://admin:pw@db:5432/app")
    monkeypatch.setenv("LOADER_DATABASE_URL", "postgresql+psycopg://loader:pw@db:5432/app")

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url.startswith("postgresql+asyncpg://readonly:")
    assert settings.migration_database_url.startswith("postgresql+psycopg://admin:")
    assert settings.loader_database_url.startswith("postgresql+psycopg://loader:")


def test_readonly_url_cannot_equal_migration_url(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = "postgresql+asyncpg://admin:pw@db:5432/app"
    monkeypatch.setenv("DATABASE_URL", shared)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", shared)
    monkeypatch.setenv("LOADER_DATABASE_URL", "postgresql+psycopg://loader:pw@db:5432/app")

    with pytest.raises(ValidationError, match="must use different credentials"):
        DatabaseSettings(_env_file=None)
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run:

```bash
uv run pytest tests/unit/test_config.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'governed_analytics.config'`.

- [ ] **Step 3: Implement immutable database settings**

Create `src/governed_analytics/config.py`:

```python
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str
    migration_database_url: str
    loader_database_url: str

    @model_validator(mode="after")
    def require_separate_readonly_credentials(self) -> Self:
        if self.database_url == self.migration_database_url:
            raise ValueError("database_url and migration_database_url must use different credentials")
        return self
```

Extend `.env.example` with these local-only URLs:

```dotenv
POSTGRES_DB=governed_analytics
POSTGRES_USER=governed_admin
POSTGRES_PASSWORD=governed_admin_dev
MIGRATION_DATABASE_URL=postgresql+psycopg://governed_admin:governed_admin_dev@127.0.0.1:5432/governed_analytics
LOADER_DATABASE_URL=postgresql+psycopg://analytics_loader:analytics_loader_dev@127.0.0.1:5432/governed_analytics
DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics
```

Remove the older single admin `DATABASE_URL` line so there is one canonical value per variable.

- [ ] **Step 4: Add the PostgreSQL Compose service**

Create `docker-compose.yml`:

```yaml
services:
  db:
    image: pgvector/pgvector:0.8.6-pg17-bookworm
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-governed_analytics}
      POSTGRES_USER: ${POSTGRES_USER:-governed_admin}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-governed_admin_dev}
      TZ: UTC
    ports:
      - "127.0.0.1:5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 2s
      timeout: 3s
      retries: 20
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./infra/docker/postgres/init:/docker-entrypoint-initdb.d:ro

volumes:
  postgres_data:
```

Create `infra/docker/postgres/init/001_bootstrap.sql`:

```sql
create extension if not exists vector;
revoke create on schema public from public;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'analytics_loader') then
    create role analytics_loader login password 'analytics_loader_dev' nosuperuser nocreatedb nocreaterole;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'analytics_readonly') then
    create role analytics_readonly login password 'analytics_readonly_dev' nosuperuser nocreatedb nocreaterole;
  end if;
end
$$;

grant usage on schema public to analytics_loader, analytics_readonly;

do $$
begin
  execute format(
    'grant connect on database %I to analytics_loader, analytics_readonly',
    current_database()
  );
end
$$;
```

- [ ] **Step 5: Verify settings and Compose configuration**

Run:

```bash
uv run pytest tests/unit/test_config.py -v
docker compose config --quiet
```

Expected: two tests pass and Compose exits zero without printing validation errors.

- [ ] **Step 6: Commit the settings and Compose boundary**

```bash
git add .env.example pyproject.toml docker-compose.yml infra/docker/postgres/init/001_bootstrap.sql src/governed_analytics/config.py tests/unit/test_config.py
git commit -m "chore: 建立 PostgreSQL 本地环境与角色配置"
```

## Task 2: Alembic and Connection Factories

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/.gitkeep`
- Create: `src/governed_analytics/persistence/__init__.py`
- Create: `src/governed_analytics/persistence/database.py`
- Test: `tests/unit/persistence/test_database.py`

**Interfaces:**
- Consumes: `DatabaseSettings`.
- Produces: `create_async_database_engine(settings) -> AsyncEngine` and a synchronous Alembic migration environment using `MIGRATION_DATABASE_URL`.

- [ ] **Step 1: Write a failing engine configuration test**

Create `tests/unit/persistence/test_database.py`:

```python
from governed_analytics.config import DatabaseSettings
from governed_analytics.persistence.database import create_async_database_engine


def test_engine_uses_pool_pre_ping_and_bounded_pool() -> None:
    settings = DatabaseSettings(
        database_url="postgresql+asyncpg://readonly:pw@localhost:5432/app",
        migration_database_url="postgresql+psycopg://admin:pw@localhost:5432/app",
        loader_database_url="postgresql+psycopg://loader:pw@localhost:5432/app",
    )

    engine = create_async_database_engine(settings)

    assert engine.pool.size() == 5
    assert engine.pool._pre_ping is True
```

- [ ] **Step 2: Run the test and verify the missing package failure**

Run:

```bash
uv run pytest tests/unit/persistence/test_database.py -v
```

Expected: import fails because `governed_analytics.persistence.database` does not exist.

- [ ] **Step 3: Implement the async engine factory**

Create `src/governed_analytics/persistence/database.py`:

```python
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from governed_analytics.config import DatabaseSettings


def create_async_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
    )
```

Create `src/governed_analytics/persistence/__init__.py` with no exports.

- [ ] **Step 4: Initialize Alembic and make the URL environment-driven**

Run once:

```bash
uv run alembic init migrations
```

Set `script_location = %(here)s/migrations` in `alembic.ini`. Do not store a real URL in the file. In `migrations/env.py`, set the URL before creating the engine:

```python
from governed_analytics.config import DatabaseSettings

settings = DatabaseSettings()
config.set_main_option("sqlalchemy.url", settings.migration_database_url)
```

Keep `target_metadata = None`; migrations in this plan are explicit SQL contracts, not ORM autogeneration.

- [ ] **Step 5: Verify the engine and Alembic configuration**

Run:

```bash
uv run pytest tests/unit/persistence/test_database.py -v
uv run alembic heads
```

Expected: the engine test passes and `alembic heads` exits zero with no revisions yet.

- [ ] **Step 6: Commit connection infrastructure**

```bash
git add alembic.ini migrations src/governed_analytics/persistence tests/unit/persistence
git commit -m "chore: 配置数据库连接与 Alembic"
```

## Task 3: Create the Ecommerce Schema Migration

**Files:**
- Create: `migrations/versions/0001_create_ecommerce_schema.py`
- Test: `tests/integration/persistence/test_schema_contract.py`

**Interfaces:**
- Consumes: admin migration connection and the schema contract above.
- Produces: the twelve tables, constraints, and query indexes used by generators, metrics, and golden SQL.

- [ ] **Step 1: Write the failing table and extension contract test**

Create `tests/integration/persistence/test_schema_contract.py`:

```python
import os

import psycopg
import pytest

EXPECTED_TABLES = {
    "campaign_attributions",
    "categories",
    "customers",
    "inventory_snapshots",
    "marketing_campaigns",
    "order_items",
    "orders",
    "payments",
    "pipeline_runs",
    "products",
    "refunds",
    "web_sessions",
}


@pytest.mark.integration
def test_schema_contains_all_business_tables_and_vector_extension() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select tablename from pg_tables where schemaname = 'public'"
            )
        }
        vector_enabled = connection.execute(
            "select exists(select 1 from pg_extension where extname = 'vector')"
        ).fetchone()

    assert EXPECTED_TABLES <= tables
    assert vector_enabled == (True,)
```

- [ ] **Step 2: Start PostgreSQL and verify the test fails before migration**

Run:

```bash
docker compose up -d --wait db
uv run pytest tests/integration/persistence/test_schema_contract.py -v
```

Expected: the assertion reports missing business tables.

- [ ] **Step 3: Create revision `0001` with the exact schema**

Create `migrations/versions/0001_create_ecommerce_schema.py`. Set `revision = "0001"`, `down_revision = None`, and execute the following SQL in `upgrade()`:

```sql
create table categories (
  category_id bigint generated always as identity primary key,
  category_code text not null unique,
  category_name text not null,
  created_at timestamptz not null default now(),
  constraint categories_code_nonempty check (btrim(category_code) <> ''),
  constraint categories_name_nonempty check (btrim(category_name) <> '')
);

create table customers (
  customer_id bigint generated always as identity primary key,
  customer_code text not null unique,
  segment text not null,
  region text not null,
  registered_at timestamptz not null,
  constraint customers_segment_allowed check (segment in ('new', 'regular', 'vip')),
  constraint customers_region_nonempty check (btrim(region) <> '')
);

create table products (
  product_id bigint generated always as identity primary key,
  sku text not null unique,
  category_id bigint not null references categories(category_id) on delete restrict,
  product_name text not null,
  list_price numeric(14,2) not null,
  unit_cost numeric(14,2) not null,
  is_active boolean not null default true,
  constraint products_price_nonnegative check (list_price >= 0),
  constraint products_cost_nonnegative check (unit_cost >= 0),
  constraint products_cost_not_above_price check (unit_cost <= list_price)
);

create table orders (
  order_id bigint generated always as identity primary key,
  order_code text not null unique,
  customer_id bigint not null references customers(customer_id) on delete restrict,
  status text not null,
  ordered_at timestamptz not null,
  region text,
  channel text not null,
  currency text not null default 'CNY',
  gross_amount numeric(14,2) not null,
  discount_amount numeric(14,2) not null default 0,
  shipping_amount numeric(14,2) not null default 0,
  payable_amount numeric(14,2) not null,
  updated_at timestamptz not null,
  constraint orders_status_allowed check (status in ('placed', 'paid', 'completed', 'cancelled', 'refunded')),
  constraint orders_channel_allowed check (channel in ('organic', 'search', 'social', 'affiliate', 'email')),
  constraint orders_currency_cny check (currency = 'CNY'),
  constraint orders_amounts_nonnegative check (
    gross_amount >= 0 and discount_amount >= 0 and shipping_amount >= 0 and payable_amount >= 0
  )
);

create table order_items (
  order_item_id bigint generated always as identity primary key,
  source_line_id text not null,
  order_id bigint not null references orders(order_id) on delete restrict,
  product_id bigint not null references products(product_id) on delete restrict,
  quantity integer not null,
  unit_price numeric(14,2) not null,
  discount_amount numeric(14,2) not null default 0,
  gross_amount numeric(14,2) not null,
  net_amount numeric(14,2) not null,
  constraint order_items_quantity_positive check (quantity > 0),
  constraint order_items_amounts_nonnegative check (
    unit_price >= 0 and discount_amount >= 0 and gross_amount >= 0 and net_amount >= 0
  )
);

create table payments (
  payment_id bigint generated always as identity primary key,
  payment_code text not null unique,
  order_id bigint not null references orders(order_id) on delete restrict,
  status text not null,
  provider text not null,
  amount numeric(14,2) not null,
  paid_at timestamptz,
  created_at timestamptz not null,
  constraint payments_status_allowed check (status in ('pending', 'succeeded', 'failed')),
  constraint payments_provider_allowed check (provider in ('alipay', 'wechat_pay', 'card')),
  constraint payments_amount_nonnegative check (amount >= 0)
);

create table refunds (
  refund_id bigint generated always as identity primary key,
  refund_code text not null unique,
  order_id bigint not null references orders(order_id) on delete restrict,
  order_item_id bigint references order_items(order_item_id) on delete restrict,
  status text not null,
  amount numeric(14,2) not null,
  reason text not null,
  refunded_at timestamptz,
  created_at timestamptz not null,
  constraint refunds_status_allowed check (status in ('requested', 'succeeded', 'rejected')),
  constraint refunds_amount_positive check (amount > 0),
  constraint refunds_reason_nonempty check (btrim(reason) <> '')
);

create table inventory_snapshots (
  inventory_snapshot_id bigint generated always as identity primary key,
  snapshot_at timestamptz not null,
  product_id bigint not null references products(product_id) on delete restrict,
  available_qty integer not null,
  reserved_qty integer not null default 0,
  constraint inventory_snapshot_unique unique (snapshot_at, product_id),
  constraint inventory_quantities_nonnegative check (available_qty >= 0 and reserved_qty >= 0)
);

create table web_sessions (
  session_id bigint generated always as identity primary key,
  session_code text not null unique,
  customer_id bigint references customers(customer_id) on delete restrict,
  order_id bigint references orders(order_id) on delete restrict,
  channel text not null,
  occurred_at timestamptz not null,
  converted boolean not null default false,
  duration_seconds integer not null,
  constraint web_sessions_channel_allowed check (channel in ('organic', 'search', 'social', 'affiliate', 'email')),
  constraint web_sessions_duration_nonnegative check (duration_seconds >= 0),
  constraint web_sessions_conversion_order check (not converted or order_id is not null)
);

create table marketing_campaigns (
  campaign_id bigint generated always as identity primary key,
  campaign_code text not null unique,
  campaign_name text not null,
  channel text not null,
  start_at timestamptz not null,
  end_at timestamptz not null,
  spend numeric(14,2) not null,
  constraint campaigns_channel_allowed check (channel in ('search', 'social', 'affiliate', 'email')),
  constraint campaigns_dates_ordered check (end_at > start_at),
  constraint campaigns_spend_nonnegative check (spend >= 0)
);

create table campaign_attributions (
  attribution_id bigint generated always as identity primary key,
  campaign_id bigint not null references marketing_campaigns(campaign_id) on delete restrict,
  order_id bigint not null references orders(order_id) on delete restrict,
  attributed_revenue numeric(14,2) not null,
  attributed_at timestamptz not null,
  constraint campaign_order_unique unique (campaign_id, order_id),
  constraint attribution_revenue_nonnegative check (attributed_revenue >= 0)
);

create table pipeline_runs (
  pipeline_run_id bigint generated always as identity primary key,
  pipeline_name text not null,
  started_at timestamptz not null,
  finished_at timestamptz,
  status text not null,
  watermark timestamptz,
  row_count bigint,
  error_code text,
  constraint pipeline_status_allowed check (status in ('running', 'succeeded', 'failed')),
  constraint pipeline_row_count_nonnegative check (row_count is null or row_count >= 0),
  constraint pipeline_finish_after_start check (finished_at is null or finished_at >= started_at)
);

create index products_category_id_idx on products (category_id);
create index customers_segment_idx on customers (segment);
create index customers_region_idx on customers (region);
create index orders_customer_ordered_idx on orders (customer_id, ordered_at);
create index orders_status_ordered_idx on orders (status, ordered_at);
create index orders_region_ordered_idx on orders (region, ordered_at);
create index orders_channel_ordered_idx on orders (channel, ordered_at);
create index order_items_order_id_idx on order_items (order_id);
create index order_items_product_id_idx on order_items (product_id);
create index order_items_source_line_id_idx on order_items (source_line_id);
create index payments_order_status_idx on payments (order_id, status);
create index payments_paid_at_idx on payments (paid_at) where status = 'succeeded';
create index refunds_order_id_idx on refunds (order_id);
create index refunds_order_item_id_idx on refunds (order_item_id);
create index refunds_refunded_at_idx on refunds (refunded_at) where status = 'succeeded';
create index inventory_product_snapshot_idx on inventory_snapshots (product_id, snapshot_at desc);
create index web_sessions_customer_occurred_idx on web_sessions (customer_id, occurred_at);
create index web_sessions_order_id_idx on web_sessions (order_id);
create index web_sessions_channel_occurred_idx on web_sessions (channel, occurred_at);
create index campaigns_channel_start_idx on marketing_campaigns (channel, start_at);
create index attributions_campaign_id_idx on campaign_attributions (campaign_id);
create index attributions_order_id_idx on campaign_attributions (order_id);
create index pipeline_name_started_idx on pipeline_runs (pipeline_name, started_at desc);
```

The `downgrade()` SQL drops tables in this exact order:

```sql
drop table if exists campaign_attributions;
drop table if exists pipeline_runs;
drop table if exists web_sessions;
drop table if exists inventory_snapshots;
drop table if exists refunds;
drop table if exists payments;
drop table if exists order_items;
drop table if exists orders;
drop table if exists marketing_campaigns;
drop table if exists products;
drop table if exists customers;
drop table if exists categories;
```

- [ ] **Step 4: Apply the migration and pass the table contract test**

Run:

```bash
uv run alembic upgrade head
uv run pytest tests/integration/persistence/test_schema_contract.py -v
```

Expected: the test passes and confirms all twelve tables plus `vector`.

- [ ] **Step 5: Commit the schema migration**

```bash
git add migrations/versions/0001_create_ecommerce_schema.py tests/integration/persistence/test_schema_contract.py
git commit -m "feat: 创建电商分析数据库结构"
```

## Task 4: Least-Privilege Grants

**Files:**
- Create: `migrations/versions/0002_grant_analytics_roles.py`
- Create: `tests/integration/persistence/test_database_roles.py`

**Interfaces:**
- Consumes: the twelve tables from revision `0001` and roles from `001_bootstrap.sql`.
- Produces: loader DML access and read-only SELECT access, both enforced by PostgreSQL.

- [ ] **Step 1: Write failing loader/read-only privilege tests**

Create `tests/integration/persistence/test_database_roles.py`:

```python
import os

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


@pytest.mark.integration
def test_readonly_role_can_select_but_cannot_create_or_insert() -> None:
    url = os.environ["DATABASE_URL"].replace("+asyncpg", "")
    with psycopg.connect(url) as connection:
        assert connection.execute("select count(*) from categories").fetchone() == (0,)
        with pytest.raises(InsufficientPrivilege):
            connection.execute(
                "insert into categories (category_code, category_name) values ('x', 'x')"
            )
        connection.rollback()
        with pytest.raises(InsufficientPrivilege):
            connection.execute("create table forbidden_table (id bigint)")


@pytest.mark.integration
def test_loader_role_can_insert_but_cannot_create_tables() -> None:
    url = os.environ["LOADER_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as connection:
        inserted = connection.execute(
            "insert into categories (category_code, category_name) values ('test', 'Test') returning category_id"
        ).fetchone()
        assert inserted is not None
        connection.rollback()
        with pytest.raises(InsufficientPrivilege):
            connection.execute("create table forbidden_loader_table (id bigint)")
```

- [ ] **Step 2: Run the role tests and verify privilege failures occur too early**

Run:

```bash
uv run pytest tests/integration/persistence/test_database_roles.py -v
```

Expected: both roles fail their permitted operations because table grants do not exist yet.

- [ ] **Step 3: Add revision `0002` with explicit grants**

Create `migrations/versions/0002_grant_analytics_roles.py` with `revision = "0002"` and `down_revision = "0001"`. Execute in `upgrade()`:

```sql
grant usage on schema public to analytics_loader, analytics_readonly;
grant select, insert, update, delete, truncate on all tables in schema public to analytics_loader;
grant usage, select on all sequences in schema public to analytics_loader;
grant select on all tables in schema public to analytics_readonly;

alter default privileges in schema public
grant select, insert, update, delete, truncate on tables to analytics_loader;
alter default privileges in schema public
grant usage, select on sequences to analytics_loader;
alter default privileges in schema public
grant select on tables to analytics_readonly;
```

Execute in `downgrade()`:

```sql
alter default privileges in schema public revoke select on tables from analytics_readonly;
alter default privileges in schema public revoke usage, select on sequences from analytics_loader;
alter default privileges in schema public revoke select, insert, update, delete, truncate on tables from analytics_loader;
revoke select on all tables in schema public from analytics_readonly;
revoke usage, select on all sequences in schema public from analytics_loader;
revoke select, insert, update, delete, truncate on all tables in schema public from analytics_loader;
```

- [ ] **Step 4: Apply the grant migration and pass the role tests**

Run:

```bash
uv run alembic upgrade head
uv run pytest tests/integration/persistence/test_database_roles.py -v
```

Expected: read-only SELECT and loader INSERT pass; DDL attempts raise `InsufficientPrivilege`.

- [ ] **Step 5: Commit role enforcement**

```bash
git add migrations/versions/0002_grant_analytics_roles.py tests/integration/persistence/test_database_roles.py
git commit -m "feat: 强制数据库最小权限角色"
```

## Task 5: Index and Migration Contract Tests

**Files:**
- Modify: `tests/integration/persistence/test_schema_contract.py`
- Create: `tests/integration/persistence/test_migrations.py`

**Interfaces:**
- Consumes: Alembic revisions `0001` and `0002`.
- Produces: automated evidence that every foreign key is indexed and migration head is `0002`.

- [ ] **Step 1: Add a foreign-key index test**

Append to `test_schema_contract.py`:

```python
@pytest.mark.integration
def test_every_foreign_key_column_is_indexed() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    query = """
        select conrelid::regclass::text, a.attname
        from pg_constraint c
        join pg_attribute a
          on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
        where c.contype = 'f'
          and not exists (
            select 1
            from pg_index i
            where i.indrelid = c.conrelid
              and a.attnum = any(i.indkey)
          )
        order by 1, 2
    """
    with psycopg.connect(url) as connection:
        missing = connection.execute(query).fetchall()

    assert missing == []
```

- [ ] **Step 2: Add an Alembic head test**

Create `tests/integration/persistence/test_migrations.py`:

```python
import os

import psycopg
import pytest


@pytest.mark.integration
def test_database_is_at_expected_alembic_head() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert revision == ("0002",)
```

- [ ] **Step 3: Run all persistence integration tests**

Run:

```bash
uv run pytest tests/integration/persistence -v
uv run alembic current
```

Expected: all tests pass and Alembic prints `0002 (head)`.

- [ ] **Step 4: Verify downgrade/upgrade on the disposable local database**

Run only before loading generated data:

```bash
uv run alembic downgrade base
uv run alembic upgrade head
uv run pytest tests/integration/persistence -v
```

Expected: both migrations replay successfully and all persistence tests pass.

- [ ] **Step 5: Commit schema contract coverage**

```bash
git add tests/integration/persistence
git commit -m "test: 验证数据库迁移与外键索引"
```

## Task 6: Developer Commands and CI Gate

**Files:**
- Modify: `Makefile`
- Create: `.github/workflows/ci.yml`
- Create: `docs/database.md`
- Modify: `README.md`
- Move: `tests/test_project_bootstrap.py` to `tests/unit/test_project_bootstrap.py`

**Interfaces:**
- Consumes: Compose, Alembic, and persistence tests.
- Produces: stable developer commands and a network-free CI gate.

- [ ] **Step 1: Add local database commands**

Move the bootstrap test under `tests/unit/`, replace the existing `test` target so it runs unit tests only, and add these database targets to `Makefile`:

```make
.PHONY: db-up db-down migrate migration-check test-integration

test:
	@uv run pytest tests/unit

db-up:
	@docker compose up -d --wait db

db-down:
	@docker compose stop db

migrate:
	@uv run alembic upgrade head

migration-check:
	@uv run alembic current
	@uv run pytest tests/integration/persistence/test_migrations.py -q

test-integration:
	@uv run pytest tests/integration -v
```

`db-down` stops the container but preserves the named volume. Do not add a default target that calls `docker compose down -v`.

- [ ] **Step 2: Add GitHub Actions without paid model calls**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
      - run: uv sync --locked --all-groups
      - run: uv run ruff check .
      - run: uv run mypy
      - run: uv run pytest tests/unit -v

  database:
    runs-on: ubuntu-latest
    env:
      POSTGRES_DB: governed_analytics
      POSTGRES_USER: governed_admin
      POSTGRES_PASSWORD: governed_admin_dev
      MIGRATION_DATABASE_URL: postgresql+psycopg://governed_admin:governed_admin_dev@127.0.0.1:5432/governed_analytics
      LOADER_DATABASE_URL: postgresql+psycopg://analytics_loader:analytics_loader_dev@127.0.0.1:5432/governed_analytics
      DATABASE_URL: postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v10
      - run: uv sync --locked --all-groups
      - run: docker compose up -d --wait db
      - run: uv run alembic upgrade head
      - run: uv run pytest tests/integration/persistence -v
```

- [ ] **Step 3: Document role and migration boundaries**

Create `docs/database.md` with:

- the three connection URLs and which commands may use each;
- the twelve-table schema diagram or table list;
- `make db-up`, `make migrate`, `make test-integration`, and `make db-down`;
- the rule that only Alembic changes schema;
- the rule that Agent queries always use `analytics_readonly`;
- a warning that local example passwords are development-only.

Add a Database section to `README.md` linking to `docs/database.md`.

- [ ] **Step 4: Run the complete Plan A gate**

Run:

```bash
make doctor
make check
make db-up
make migrate
make migration-check
uv run pytest tests/integration/persistence -v
git diff --check
```

Expected: zero environment errors, quality checks pass, database is healthy at revision `0002`, persistence tests pass, and no whitespace errors are reported.

- [ ] **Step 5: Commit Plan A developer workflow**

```bash
git add Makefile .github/workflows/ci.yml docs/database.md README.md tests/test_project_bootstrap.py tests/unit/test_project_bootstrap.py
git commit -m "ci: 验证数据库迁移与权限边界"
```

## Plan A Completion Gate

- [ ] `docker compose up -d --wait db` succeeds on arm64 and CI amd64.
- [ ] `vector` extension exists.
- [ ] Exactly twelve business tables exist after `alembic upgrade head`.
- [ ] Every foreign key column is covered by an index.
- [ ] `analytics_readonly` can SELECT and cannot perform DML or DDL.
- [ ] `analytics_loader` can load data and cannot perform DDL.
- [ ] `uv run alembic downgrade base && uv run alembic upgrade head` succeeds on the disposable empty database.
- [ ] Unit and persistence integration tests pass.
- [ ] No paid API or model key is used.
