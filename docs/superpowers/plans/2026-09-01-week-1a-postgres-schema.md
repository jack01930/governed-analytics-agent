# 第 1A 周：PostgreSQL Schema 实现计划

> **状态同步（2026-09-04）：** 实现步骤已完成；本地 arm64 上数据库迁移、12 表 Schema、角色权限与集成
> 测试通过，当前 head 为后续加入数据集重置函数的 `0003`。外部 CI amd64 尚无运行记录，相关项保持待验证。

> **供 Agent 执行者使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务实施本计划。各步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 交付一个 Docker 化的 PostgreSQL 17 数据库，包含 pgvector、12 张电商业务表、确定性 Alembic 迁移、最小权限的加载/只读角色，以及可执行的 Schema 契约测试。

**架构：** Docker Compose 管理本地数据库生命周期。仅由管理员迁移连接执行的 Alembic 迁移，是 Schema 的唯一真值来源。应用代码使用异步 SQLAlchemy 连接；批量加载使用专用加载角色；分析查询和未来 Agent 查询使用经过独立测试的只读角色。

**技术栈：** Docker Compose、`pgvector/pgvector:0.8.6-pg17-bookworm`、PostgreSQL 17、SQLAlchemy 2、Alembic 1.19、asyncpg、psycopg 3、Pydantic Settings、Pytest。

**规格依据：** `GOVERNED_ANALYTICS_AGENT_PLAN.md` 第 18–21、26、38–39 节，以及 `docs/superpowers/plans/2026-09-01-week-1-data-baseline.md`。

## 全局约束

- 使用小写 `snake_case` 标识符，不使用带引号的混合大小写名称。
- 主键使用 `bigint generated always as identity`。
- 字符串使用 `text`，时间点使用 `timestamptz`，业务日期使用 `date`，人民币金额使用 `numeric(14,2)`，标志位使用 `boolean`。
- 为每个外键建立索引；复合索引中等值列位于范围列之前。
- 使用数据库约束保证结构有效，但不得添加会阻止固定种子数据质量异常的跨表约束。
- `analytics_readonly` 仅获得 `CONNECT`、Schema `USAGE` 和表 `SELECT` 权限。
- `analytics_loader` 获得表 DML、`TRUNCATE` 及序列使用权限，用于确定性地重建本地数据集，但不拥有 DDL 权限。
- 管理员 URL 仅供 Alembic 和本地初始化命令使用。
- 测试可以重建临时 CI 数据库；本计划任何命令默认都不得删除开发者的命名 Docker 卷。

---

## Schema 契约

| 表 | 主键 | 必需外键 | 有意允许的异常 |
|---|---|---|---|
| `customers` | `customer_id bigint identity` | 无 | 无 |
| `categories` | `category_id bigint identity` | 无 | 无 |
| `products` | `product_id bigint identity` | `category_id -> categories` | 无 |
| `orders` | `order_id bigint identity` | `customer_id -> customers` | 为完整性用例允许 `region` 为空 |
| `order_items` | `order_item_id bigint identity` | `order_id -> orders`、`product_id -> products` | `source_line_id` 不唯一，因此可注入逻辑重复行 |
| `payments` | `payment_id bigint identity` | `order_id -> orders` | 为一致性用例允许合计金额与 `orders` 不一致 |
| `refunds` | `refund_id bigint identity` | `order_id -> orders`、可空 `order_item_id -> order_items` | 为质量用例允许退款额超过成功支付额 |
| `inventory_snapshots` | `inventory_snapshot_id bigint identity` | `product_id -> products` | 以缺失近期快照表示新鲜度故障 |
| `web_sessions` | `session_id bigint identity` | 可空 `customer_id -> customers`、可空 `order_id -> orders` | 匿名会话有效 |
| `marketing_campaigns` | `campaign_id bigint identity` | 无 | 无 |
| `campaign_attributions` | `attribution_id bigint identity` | `campaign_id -> marketing_campaigns`、`order_id -> orders` | 无 |
| `pipeline_runs` | `pipeline_run_id bigint identity` | 无 | 失败运行可具有空 `finished_at` 和陈旧 watermark |

业务事实表使用 `restrict` 删除策略。合成数据集整体重建；应用代码不得级联删除分析历史。

## 任务 1：数据库设置与 Compose 服务

**文件：**
- 新建：`src/governed_analytics/config.py`
- 新建：`tests/unit/test_config.py`
- 新建：`docker-compose.yml`
- 新建：`infra/docker/postgres/init/001_bootstrap.sql`
- 修改：`.env.example`
- 修改：`pyproject.toml`

**接口：**
- 输入：`.env.example` 中记录的环境变量。
- 输出：只读专用 `DatabaseSettings`、加载专用 `LoaderDatabaseSettings`、迁移专用 `MigrationDatabaseSettings`、名为 `db` 的健康 Compose 服务，以及 `analytics_loader` 与 `analytics_readonly` 角色。

- [x] **步骤 1：注册 Pytest 标记并编写失败的设置测试**

在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 中添加：

```toml
markers = [
  "integration: requires local Docker services",
  "live_model: requires explicit paid model authorization",
]
```

创建 `tests/unit/test_config.py`：

```python
import pytest
from pydantic import ValidationError

from governed_analytics.config import (
    DatabaseSettings,
    LoaderDatabaseSettings,
    MigrationDatabaseSettings,
)


def test_role_scoped_settings_load_only_their_own_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://analytics_readonly:pw@db:5432/app"
    )
    monkeypatch.setenv(
        "LOADER_DATABASE_URL", "postgresql+psycopg://analytics_loader:pw@db:5432/app"
    )
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", "postgresql+psycopg://governed_admin:pw@db:5432/app"
    )

    assert set(DatabaseSettings(_env_file=None).model_dump()) == {"database_url"}
    assert set(LoaderDatabaseSettings(_env_file=None).model_dump()) == {
        "loader_database_url"
    }
    assert set(MigrationDatabaseSettings(_env_file=None).model_dump()) == {
        "migration_database_url"
    }


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://analytics_readonly:pw@db:5432/app",
        "postgresql+asyncpg://analytics_loader:pw@db:5432/app",
        "postgresql+asyncpg://%61nalytics_readonly:pw@db:5432/app",
        "postgresql+asyncpg://analytics_readonly:@db:5432/app",
    ],
)
def test_readonly_settings_reject_invalid_driver_role_or_password(database_url: str) -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(database_url=database_url)
```

为 `LoaderDatabaseSettings` 和 `MigrationDatabaseSettings` 添加等价的角色互换、driver、编码角色名及缺失密码拒绝矩阵。按 URL 语义解析凭据，并在验证前进行百分号解码；但要求使用规范的未转义用户名写法，以拒绝编码后等价的角色名。driver 与角色身份必须精确匹配：`postgresql+asyncpg` / `analytics_readonly`、`postgresql+psycopg` / `analytics_loader`，以及 `postgresql+psycopg` / `governed_admin`。每个 URL 都必须包含显式非空密码。

- [x] **步骤 2：运行测试并确认因模块缺失而失败**

运行：

```bash
uv run pytest tests/unit/test_config.py -v
```

预期：由于三个按角色隔离的设置契约尚不存在，测试收集失败。

- [x] **步骤 3：实现不可变数据库设置**

创建 `src/governed_analytics/config.py`。共享的不可变 `SettingsConfigDict` 与 URL 验证辅助函数保持私有；每个公共设置类只声明自己的 URL：

```python
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _DatabaseSettingsBase(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )


class DatabaseSettings(_DatabaseSettingsBase):
    database_url: str


class LoaderDatabaseSettings(_DatabaseSettingsBase):
    loader_database_url: str


class MigrationDatabaseSettings(_DatabaseSettingsBase):
    migration_database_url: str
```

通过私有辅助函数应用字段验证器，实现上述精确 driver、规范固定用户名及解码后非空密码契约。不提供合并式兼容对象：任何消费者都不能实例化包含其他角色秘密的设置。

在 `.env.example` 中加入以下仅限本地使用的 URL：

```dotenv
POSTGRES_DB=governed_analytics
POSTGRES_USER=governed_admin
POSTGRES_PASSWORD=governed_admin_dev
MIGRATION_DATABASE_URL=postgresql+psycopg://governed_admin:governed_admin_dev@127.0.0.1:5432/governed_analytics
LOADER_DATABASE_URL=postgresql+psycopg://analytics_loader:analytics_loader_dev@127.0.0.1:5432/governed_analytics
DATABASE_URL=postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics
```

删除旧的单一管理员 `DATABASE_URL` 行，确保每个变量只有一个规范值。

- [x] **步骤 4：添加 PostgreSQL Compose 服务**

创建 `docker-compose.yml`：

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

创建 `infra/docker/postgres/init/001_bootstrap.sql`：

```sql
create extension if not exists vector;
revoke create on schema public from public;

do $$
begin
  execute format('revoke temporary on database %I from public', current_database());
end
$$;

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

- [x] **步骤 5：验证设置与 Compose 配置**

运行：

```bash
uv run pytest tests/unit/test_config.py -v
docker compose config --quiet
```

预期：按角色隔离的设置矩阵通过，Compose 以状态码 0 退出且不打印验证错误。

- [x] **步骤 6：提交设置与 Compose 边界**

```bash
git add .env.example pyproject.toml docker-compose.yml infra/docker/postgres/init/001_bootstrap.sql src/governed_analytics/config.py tests/unit/test_config.py
git commit -m "chore: 建立 PostgreSQL 本地环境与角色配置"
```

## 任务 2：Alembic 与连接工厂

**文件：**
- 新建：`alembic.ini`
- 新建：`migrations/env.py`
- 新建：`migrations/script.py.mako`
- 新建：`migrations/versions/.gitkeep`
- 新建：`src/governed_analytics/persistence/__init__.py`
- 新建：`src/governed_analytics/persistence/database.py`
- 测试：`tests/unit/persistence/test_database.py`

**接口：**
- 输入：异步应用 engine 使用只读专用 `DatabaseSettings`；Alembic 使用迁移专用 `MigrationDatabaseSettings`。
- 输出：`create_async_database_engine(settings) -> AsyncEngine`、`set_alembic_database_url(config, database_url) -> None`，以及只使用 `MIGRATION_DATABASE_URL` 的同步 Alembic 迁移环境。

- [x] **步骤 1：编写失败的 engine 配置测试**

创建 `tests/unit/persistence/test_database.py`：

```python
from alembic.config import Config

from governed_analytics.config import DatabaseSettings
from governed_analytics.persistence.database import (
    create_async_database_engine,
    set_alembic_database_url,
)


def test_engine_uses_pool_pre_ping_and_bounded_pool() -> None:
    settings = DatabaseSettings(
        database_url="postgresql+asyncpg://analytics_readonly:pw@localhost:5432/app",
    )

    engine = create_async_database_engine(settings)

    assert engine.pool.size() == 5
    assert engine.pool._pre_ping is True


def test_alembic_database_url_round_trips_percent_encoded_credentials() -> None:
    config = Config()
    url = "postgresql+psycopg://governed_admin:p%40ss%25word@localhost:5432/app"

    set_alembic_database_url(config, url)

    assert config.get_main_option("sqlalchemy.url") == url
```

- [x] **步骤 2：运行测试并确认因包缺失而失败**

运行：

```bash
uv run pytest tests/unit/persistence/test_database.py -v
```

预期：由于 `governed_analytics.persistence.database` 不存在，导入失败。

- [x] **步骤 3：实现异步 engine 工厂**

创建 `src/governed_analytics/persistence/database.py`：

```python
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from alembic.config import Config

from governed_analytics.config import DatabaseSettings


def create_async_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
    )


def set_alembic_database_url(config: Config, database_url: str) -> None:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
```

创建不导出任何符号的 `src/governed_analytics/persistence/__init__.py`。

- [x] **步骤 4：初始化 Alembic，并让 URL 由环境驱动**

运行一次：

```bash
uv run alembic init migrations
```

在 `alembic.ini` 中设置 `script_location = %(here)s/migrations`。文件中不得保存真实 URL。在 `migrations/env.py` 创建 engine 前设置 URL：

```python
from governed_analytics.config import MigrationDatabaseSettings
from governed_analytics.persistence.database import set_alembic_database_url

settings = MigrationDatabaseSettings()
set_alembic_database_url(config, settings.migration_database_url)
```

辅助函数负责 `Config.set_main_option` 的插值边界：仅在持久化时将 `%` 加倍，使 `Config.get_main_option` 返回原始 URL。保持 `target_metadata = None`；本计划中的迁移是显式 SQL 契约，不使用 ORM 自动生成。

- [x] **步骤 5：验证 engine 与 Alembic 配置**

运行：

```bash
uv run pytest tests/unit/persistence/test_database.py -v
uv run alembic heads
```

预期：engine 与 `%40`/`%25` URL 往返测试通过；`alembic heads` 以状态码 0 退出，且尚无迁移版本。

- [x] **步骤 6：提交连接基础设施**

```bash
git add alembic.ini migrations src/governed_analytics/persistence tests/unit/persistence
git commit -m "chore: 配置数据库连接与 Alembic"
```

## 任务 3：创建电商 Schema 迁移

**文件：**
- 新建：`migrations/versions/0001_create_ecommerce_schema.py`
- 测试：`tests/integration/persistence/test_schema_contract.py`

**接口：**
- 输入：管理员迁移连接及上述 Schema 契约。
- 输出：生成器、指标和黄金 SQL 使用的 12 张表、约束与查询索引。

- [x] **步骤 1：编写失败的表与扩展契约测试**

创建 `tests/integration/persistence/test_schema_contract.py`：

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

- [x] **步骤 2：启动 PostgreSQL，并确认测试在迁移前失败**

运行：

```bash
docker compose up -d --wait db
uv run pytest tests/integration/persistence/test_schema_contract.py -v
```

预期：断言报告业务表缺失。

- [x] **步骤 3：使用精确 Schema 创建版本 `0001`**

创建 `migrations/versions/0001_create_ecommerce_schema.py`。设置 `revision = "0001"`、`down_revision = None`，并在 `upgrade()` 中执行以下 SQL：

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

`downgrade()` SQL 按以下精确顺序删除表：

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

- [x] **步骤 4：应用迁移并通过表契约测试**

运行：

```bash
uv run alembic upgrade head
uv run pytest tests/integration/persistence/test_schema_contract.py -v
```

预期：测试通过，并确认 12 张表及 `vector` 扩展全部存在。

- [x] **步骤 5：提交 Schema 迁移**

```bash
git add migrations/versions/0001_create_ecommerce_schema.py tests/integration/persistence/test_schema_contract.py
git commit -m "feat: 创建电商分析数据库结构"
```

## 任务 4：最小权限授权

**文件：**
- 新建：`migrations/versions/0002_grant_analytics_roles.py`
- 新建：`tests/integration/persistence/test_database_roles.py`

**接口：**
- 输入：版本 `0001` 的 12 张表，以及 `001_bootstrap.sql` 创建的角色。
- 输出：由 PostgreSQL 强制执行的加载角色 DML 权限和只读角色 SELECT 权限。

- [x] **步骤 1：编写失败的加载/只读权限测试**

创建 `tests/integration/persistence/test_database_roles.py`：

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

- [x] **步骤 2：运行角色测试，并确认授权前预期操作也会失败**

运行：

```bash
uv run pytest tests/integration/persistence/test_database_roles.py -v
```

预期：由于表授权尚不存在，两个角色连各自应被允许的操作也会失败。

- [x] **步骤 3：添加包含显式授权的版本 `0002`**

创建 `migrations/versions/0002_grant_analytics_roles.py`，设置 `revision = "0002"`、`down_revision = "0001"`。在 `upgrade()` 中执行：

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

在 `downgrade()` 中执行：

```sql
alter default privileges in schema public revoke select on tables from analytics_readonly;
alter default privileges in schema public revoke usage, select on sequences from analytics_loader;
alter default privileges in schema public revoke select, insert, update, delete, truncate on tables from analytics_loader;
revoke select on all tables in schema public from analytics_readonly;
revoke usage, select on all sequences in schema public from analytics_loader;
revoke select, insert, update, delete, truncate on all tables in schema public from analytics_loader;
```

- [x] **步骤 4：应用授权迁移并通过角色测试**

运行：

```bash
uv run alembic upgrade head
uv run pytest tests/integration/persistence/test_database_roles.py -v
```

预期：只读角色 SELECT 和加载角色 INSERT 通过；DDL 尝试抛出 `InsufficientPrivilege`。

- [x] **步骤 5：提交角色权限约束**

```bash
git add migrations/versions/0002_grant_analytics_roles.py tests/integration/persistence/test_database_roles.py
git commit -m "feat: 强制数据库最小权限角色"
```

## 任务 5：索引与迁移契约测试

**文件：**
- 修改：`tests/integration/persistence/test_schema_contract.py`
- 新建：`tests/integration/persistence/test_migrations.py`

**接口：**
- 输入：Alembic 版本 `0001`、`0002` 与后续数据集重置版本 `0003`。
- 输出：证明每个外键均已建立索引、当前迁移 head 为 `0003` 的自动化证据。

- [x] **步骤 1：添加外键索引测试**

追加到 `test_schema_contract.py`：

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

- [x] **步骤 2：添加 Alembic head 测试**

创建 `tests/integration/persistence/test_migrations.py`：

```python
import os

import psycopg
import pytest


@pytest.mark.integration
def test_database_is_at_expected_alembic_head() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert revision == ("0003",)
```

- [x] **步骤 3：运行全部持久化集成测试**

运行：

```bash
uv run pytest tests/integration/persistence -v
uv run alembic current
```

预期：全部测试通过，且 Alembic 输出 `0003 (head)`。

- [x] **步骤 4：在可丢弃的本地数据库上验证降级/升级**

仅在加载生成数据前运行：

```bash
uv run alembic downgrade base
uv run alembic upgrade head
uv run pytest tests/integration/persistence -v
```

预期：两个迁移均成功重放，全部持久化测试通过。

- [x] **步骤 5：提交 Schema 契约覆盖**

```bash
git add tests/integration/persistence
git commit -m "test: 验证数据库迁移与外键索引"
```

## 任务 6：开发命令与 CI 门禁

**文件：**
- 修改：`Makefile`
- 新建：`.github/workflows/ci.yml`
- 新建：`docs/database.md`
- 修改：`README.md`
- 移动：`tests/test_project_bootstrap.py` 至 `tests/unit/test_project_bootstrap.py`

**接口：**
- 输入：Compose、Alembic 与持久化测试。
- 输出：稳定的开发命令及无网络 CI 门禁。

- [x] **步骤 1：添加本地数据库命令**

将启动测试移到 `tests/unit/` 下，替换现有 `test` target，使其只运行单元测试，并在 `Makefile` 中加入以下数据库 target：

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

`db-down` 停止容器但保留命名卷。不得添加会调用 `docker compose down -v` 的默认 target。

- [x] **步骤 2：添加不调用付费模型的 GitHub Actions**

创建 `.github/workflows/ci.yml`：

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

- [x] **步骤 3：记录角色与迁移边界**

创建 `docs/database.md`，内容包括：

- 三个连接 URL 及各自允许使用的命令；
- 12 表 Schema 图或表清单；
- `make db-up`、`make migrate`、`make test-integration` 和 `make db-down`；
- 只有 Alembic 可以修改 Schema 的规则；
- Agent 查询始终使用 `analytics_readonly` 的规则；
- 本地示例密码仅限开发环境的警告。

在 `README.md` 中添加数据库章节，并链接到 `docs/database.md`。

- [x] **步骤 4：运行完整 Plan A 门禁**

运行：

```bash
make doctor
make check
make db-up
make migrate
make migration-check
uv run pytest tests/integration/persistence -v
git diff --check
```

预期：环境错误为零、质量检查通过、数据库健康且位于版本 `0003`、持久化测试通过，并且不报告空白字符错误。

- [x] **步骤 5：提交 Plan A 开发工作流**

```bash
git add Makefile .github/workflows/ci.yml docs/database.md README.md tests/test_project_bootstrap.py tests/unit/test_project_bootstrap.py
git commit -m "ci: 验证数据库迁移与权限边界"
```

## Plan A 完成门禁

- [ ] `docker compose up -d --wait db` 在 arm64 和 CI amd64 上均成功（arm64 已通过；外部 CI 待运行）。
- [x] `vector` 扩展存在。
- [x] 执行 `alembic upgrade head` 后恰好存在 12 张业务表。
- [x] 每个外键列都由索引覆盖。
- [x] `analytics_readonly` 可以执行 SELECT，但不能执行 DML 或 DDL。
- [x] `analytics_loader` 可以加载数据，但不能执行 DDL。
- [x] `uv run alembic downgrade base && uv run alembic upgrade head` 可在可丢弃的空数据库上成功执行。
- [x] 单元测试与持久化集成测试通过。
- [x] Plan A 未使用付费 API 或模型 Key。
