# 数据库开发指南

本项目的本地 PostgreSQL 服务由 Docker Compose 提供，Schema 仅由 Alembic 迁移管理。请勿用应用代码、手工 SQL 或容器初始化脚本改变业务 Schema。

## 连接角色和用途

| 角色 | 连接 URL | 允许用途 |
| --- | --- | --- |
| `governed_admin` | `postgresql+psycopg://governed_admin:governed_admin_dev@127.0.0.1:5432/governed_analytics` | 仅供 Alembic 迁移命令使用：`make migrate`。 |
| `analytics_loader` | `postgresql+psycopg://analytics_loader:analytics_loader_dev@127.0.0.1:5432/governed_analytics` | 数据加载程序的 DML（读、写、更新、删除与截断）；不得执行 DDL。 |
| `analytics_readonly` | `postgresql+asyncpg://analytics_readonly:analytics_readonly_dev@127.0.0.1:5432/governed_analytics` | Agent 的所有查询；只允许读取，禁止 DML 和 DDL。 |

Agent 查询始终使用 `analytics_readonly`，绝不使用迁移或加载角色。上表密码只用于本地开发；不得用于生产环境，也不得提交真实凭据。

## Schema

迁移 `0001` 创建以下 12 张业务表，迁移 `0002` 配置加载和只读权限：

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
