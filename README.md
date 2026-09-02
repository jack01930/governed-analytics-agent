# Governed Analytics Agent

面向电商运营分析师的可治理数据分析智能体，也是一个针对 Agent/大模型应用开发实习岗位设计的旗舰作品集项目。

项目已具备本地 PostgreSQL 基线、最小权限角色、可复现模拟数据与首批 15 个受治理指标。
正式需求、技术架构、评测方案和八周路线见 [项目计划设计文档](GOVERNED_ANALYTICS_AGENT_PLAN.md)。

## 项目目标

系统最终需要基于固定模拟电商数据完成多步分析：理解业务问题、检索指标口径、制定计划、安全执行只读 SQL、进行异常归因，并输出结论、证据、图表、数据质量结果和可审计轨迹。

旗舰问题：

> 本周 GMV 为什么下降？主要由哪些地区、商品和用户群导致？

## 技术基线

- Python 3.12
- FastAPI + SSE
- LangGraph
- PostgreSQL + pgvector
- SQLGlot
- Streamlit
- Docker Compose
- Pytest、Ruff、类型检查与 GitHub Actions

## 启动前检查

```bash
make doctor
```

创建本地虚拟环境：

```bash
make venv
```

复制环境变量模板后，只在本地 `.env` 中填写密钥；不得提交 `.env`：

```bash
cp .env.example .env
```

当前机器的检查结果、尚缺工具和处理建议见 [启动就绪报告](docs/STARTUP_READINESS.md)。

## 本地快速开始

```bash
cp .env.example .env
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check
make eval-fixture
```

`.env` 仅在本地使用且已被 Git 忽略；以上命令不需要模型 key 或外部 API。生成的 CSV 和
manifest 位于被忽略的 `artifacts/datasets/tiny/`。完整命令、再生安全边界与异常证据见
[数据生成指南](docs/data-generation.md)，指标公式、窗口和只读约束见
[指标指南](docs/metrics.md)。

`make eval-fixture` 只在本地 tiny 数据集上运行离线 fixture，并将不可变报告写入被 Git 忽略的
`artifacts/evals/baseline/fixture/`。该命令 100% 仅证明评测链路可用，不代表模型质量；真实模型调用仍需
明确执行双重授权命令。评测契约、报告字段、成本快照与 live 边界见[评测指南](docs/evals.md)。

## Database

本地 PostgreSQL、Alembic 迁移 `0001`–`0003`、最小权限角色和测试工作流已就绪。连接角色、
12 张业务表、可复制的开发命令及权限边界见 [数据库开发指南](docs/database.md)。

## 当前边界

- 本地开发基线包含 Docker Compose PostgreSQL、Alembic `0001`–`0003` 迁移、12 张业务表和固定
  synthetic dataset；full 数据集仅允许本地显式生成，绝不进入 CI。
- GitHub 远程仓库已配置；未经确认，不进行云端部署或公开发布。
- 未经确认，不创建云资源、不产生付费调用、不使用真实个人或企业数据。

## License

[MIT](LICENSE)
