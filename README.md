# Governed Analytics Agent

面向电商运营分析师的可治理数据分析智能体，也是一个针对 Agent/大模型应用开发实习岗位设计的旗舰作品集项目。

项目目前处于“仓库与本地开发环境已就绪、尚未开始功能实现”阶段。正式需求、技术架构、评测方案和八周路线见 [项目计划设计文档](GOVERNED_ANALYTICS_AGENT_PLAN.md)。

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

## Database

本地 PostgreSQL、Alembic 迁移、最小权限角色和测试工作流已就绪。连接角色、12 张业务表、可复制的开发命令及权限边界见 [数据库开发指南](docs/database.md)。

## 当前边界

- 本地开发基线包含 Docker Compose PostgreSQL、Alembic `0001`/`0002` 迁移和 12 张业务表；应用功能仍按项目路线逐步实现。
- GitHub 远程仓库已配置；未经确认，不进行云端部署或公开发布。
- 未经确认，不创建云资源、不产生付费调用、不使用真实个人或企业数据。

## License

[MIT](LICENSE)
