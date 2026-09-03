# 数据库开发指南

本项目的本地 PostgreSQL 服务由 Docker Compose 提供，Schema 仅由 Alembic 迁移管理。请勿用应用代码、手工 SQL 或容器初始化脚本改变业务 Schema。

## 连接角色和用途

| 角色 | 连接 URL | 允许用途 |
| --- | --- | --- |
| `governed_admin` | `postgresql+psycopg://governed_admin:governed_admin_dev@127.0.0.1:5432/governed_analytics` | 仅供 Alembic 迁移命令使用：`make migrate`。 |
| `analytics_loader` | `postgresql+psycopg://analytics_loader:analytics_loader_dev@127.0.0.1:5432/governed_analytics` | 数据加载程序的 DML（读、写、更新、删除与截断）；不得执行 DDL。 |
| `analytics_readonly` | `postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics` | Agent 的所有查询；只允许读取，禁止 DML 和 DDL。 |

Agent 查询始终使用 `analytics_readonly`，绝不使用迁移或加载角色。上表密码只用于本地开发；不得用于生产环境，也不得提交真实凭据。

代码中的设置对象也按角色隔离：`DatabaseSettings` 只读取 `DATABASE_URL`，
`LoaderDatabaseSettings` 只读取 `LOADER_DATABASE_URL`，`MigrationDatabaseSettings` 只读取
`MIGRATION_DATABASE_URL`。三者不会互相携带其他角色的 URL。每个 URL 都会校验精确 driver、
固定用户名和显式非空密码；用户名按 URL 语义解码后验证，同时拒绝 percent-encoded 的等价
用户名写法。Alembic 在写入自身配置时会转义 `%`，因此密码中的 `%40`、`%25` 等编码可原样
round-trip。

Plan B 的指标查询，以及 Plan C 的 Oracle 与基线执行器，均在 `analytics_readonly` 连接上的
显式只读事务内执行，并设置事务局部的 `statement_timeout = '10s'` 与
`search_path = public, pg_catalog`。这些防线由真实数据库集成测试验证，不配置在通用 engine
factory 上。

## Schema

迁移 `0001` 创建以下 12 张业务表，`0002` 配置加载和只读权限，`0003` 为 `analytics_loader` 增加受限的
`reset_analytics_dataset()` 数据集重置函数。当前 Alembic head 为 `0003`。

容器 bootstrap 会先从 `PUBLIC` 回收数据库 `TEMPORARY` 权限；`0002` downgrade 只回收本迁移
授予的表、序列和默认权限，不会重新授予 `PUBLIC TEMPORARY`，因此回退到 `0001` 后两个分析
角色仍不能创建临时表。

| 领域 | 表 |
| --- | --- |
| 商品与客户 | `categories`、`customers`、`products` |
| 订单与资金 | `orders`、`order_items`、`payments`、`refunds` |
| 库存与行为 | `inventory_snapshots`、`web_sessions` |
| 营销与运行 | `marketing_campaigns`、`campaign_attributions`、`pipeline_runs` |

## 本地命令

先从模板加载本地开发变量（不要创建或提交 `.env`）：

```bash
set -a
source .env.example
set +a
```

启动数据库并等待健康检查：

```bash
make db-up
```

应用全部 Alembic 迁移：

```bash
make migrate
```

运行需要数据库的集成测试：

```bash
make test-integration
```

停止数据库服务（保留命名 volume 中的开发数据）：

```bash
make db-down
```

只有 Alembic 可以改变 Schema；如需结构变更，创建并审查新的迁移文件，再通过 `make migrate` 应用。
