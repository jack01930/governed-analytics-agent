# Governed Analytics Agent

面向电商运营分析师的可治理数据分析智能体，也是一个针对 Agent/大模型应用开发实习岗位设计的旗舰作品集项目。

第 1 周数据与裸基线、第 2 周评测有效性与安全工具层均已完成。项目现有本地 PostgreSQL、最小权限角色、
可复现模拟数据、15 个受治理指标、冻结的 20 条 core-v1，以及由 20 条 core-v2、20 条 paraphrase、10 条
boundary 和 20 条 safety 组成的 70 例活跃评测集。DeepSeek 裸基线 v1、v2 均已原样保留；v2 正式结果为
5/20、有效 SQL 14/20、估算总成本 CNY 0.259588。Week 2 fixture 为 70/70，仅证明离线链路、安全策略与
Oracle 一致，不代表模型能力提升；Week 2 live 尚未运行。
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
make eval-week2-fixture
```

`.env` 仅在本地使用且已被 Git 忽略；以上命令不需要模型 key 或外部 API。生成的 CSV 和
manifest 位于被忽略的 `artifacts/datasets/tiny/`。完整命令、再生安全边界与异常证据见
[数据生成指南](docs/data-generation.md)，指标公式、窗口和只读约束见
[指标指南](docs/metrics.md)。

`make eval-fixture` 只在本地 tiny 数据集上运行离线 fixture，并将不可变报告写入被 Git 忽略的
`artifacts/evals/baseline/fixture/`。该命令 100% 仅证明评测链路可用，不代表模型质量；真实模型调用仍需
明确执行双重授权命令。`make eval-week2-fixture` 同样完全离线，运行 50 个业务执行例和 20 个直接安全例，
报告写入 `artifacts/evals/week2/fixture/`。评测契约、报告字段、成本快照与 live 边界见
[评测指南](docs/evals.md)。

当前 live provider 为 DeepSeek，默认使用 `deepseek-v4-flash` 的非思考模式。配置 Key、执行一次不可重试的
裸基线以及根据结果确定第 2 周优先级的步骤见 [DeepSeek live 基线运行手册](docs/live-baseline-playbook.md)；
黄金问题的分层扩展原则见 [Golden Questions 优化策略](docs/golden-questions-strategy.md)。v1 的适配器根因见
[DeepSeek 裸基线 v1 分析](docs/reports/week-1-deepseek-live-analysis-2026-09-03.md)，正式 v2 结果、逐类根因和
第 2 周优先级见 [DeepSeek 裸基线 v2 分析](docs/reports/week-1-deepseek-live-v2-analysis-2026-09-03.md)。两轮
正式 JSON/Markdown 与逐题计量已脱敏归档在 [live 证据快照](docs/reports/evidence/README.md)。

第 2 周四类工具、SQL 策略和稳定错误契约见 [安全工具层指南](docs/safe-tools.md)；Week 1 live 与 Week 2
fixture 的可比边界和下一步决策见
[Week 1 → Week 2 对比报告](docs/reports/week-1-to-week-2-comparison-2026-09-04.md)。

## Database

本地 PostgreSQL、Alembic 迁移 `0001`–`0003`、最小权限角色和测试工作流已就绪。连接角色、
12 张业务表、可复制的开发命令及权限边界见 [数据库开发指南](docs/database.md)。

## 当前边界

- 本地开发基线包含 Docker Compose PostgreSQL、Alembic `0001`–`0003` 迁移、12 张业务表和固定
  synthetic dataset；full 数据集仅允许本地显式生成，绝不进入 CI。
- GitHub 远程仓库已配置；未经确认，不进行云端部署或公开发布。
- 未经确认，不创建云资源、不产生付费调用、不使用真实个人或企业数据。
- Week 2 live 未获得新的单次付费调用授权，因此当前只保留可显式执行的 live 入口，没有发起模型请求。

## License

[MIT](LICENSE)
