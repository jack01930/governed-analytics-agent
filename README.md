# Governed Analytics Agent

面向电商运营分析师的可治理数据分析智能体，也是一个针对 Agent/大模型应用开发实习岗位设计的旗舰作品集项目。

第 1 周数据与裸基线、第 2 周安全工具层、第 3 周 Agent 与 API 的核心工程已完成。当前支持 15 个受治理指标、
有界多步分析、GMV 区域/SKU/客户分群归因、证据引用、预算控制、FastAPI 和 SSE。第三周收尾记录、
验证结果和遗留问题见 [第三周交付记录](docs/reports/week-3-closeout-2026-09-07.md)。

最新 Week 3 live 开发回归为 34/36，评分异常为零；它与 Week 1、Week 2 题集不同，不能据此计算跨周准确率提升。
现有题目均作为开发/回归材料。各候选完成后或求职前，再用一套新的固定题集统一评测，见
[评测隔离规则](docs/static-benchmark-policy.md)。正式需求、技术架构和八周路线见
[项目计划设计文档](GOVERNED_ANALYTICS_AGENT_PLAN.md)。

## 项目目标

系统最终需要基于固定模拟电商数据完成多步分析：理解业务问题、检索指标口径、制定计划、安全执行只读 SQL、进行异常归因，并输出结论、证据、图表、数据质量结果和可审计轨迹。

旗舰问题：

> 本周 GMV 为什么下降？主要由哪些地区、商品和用户群导致？

## 技术基线与规划

- Python 3.12
- FastAPI + SSE
- LangGraph
- PostgreSQL（pgvector/RAG 留待后续阶段）
- SQLGlot
- Streamlit（第四周待实现）
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
test -f .env || cp .env.example .env
```

当前机器的就绪记录和后续事项见 [启动就绪报告](docs/STARTUP_READINESS.md)。

## 本地快速开始

```bash
test -f .env || cp .env.example .env
uv sync --locked --all-groups
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check
make eval-fixture
make eval-week2-fixture
make eval-week3-fixture
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

本地 Agent API 可在上述 tiny 数据与迁移就绪后启动：

```bash
AGENT_RUNTIME_MODE=fixture AGENT_LIVE_ENABLED=false \
  uv run uvicorn governed_analytics.api.app:app --host 127.0.0.1 --port 8000
```

`make eval-week3-fixture` 离线运行固定的 40 个 Week 3 Agent 用例，并把本次唯一 `report.json` 的绝对路径
原子写入 `artifacts/evals/week3/fixture-report-path.txt`。它验证 LangGraph、四类安全工具、只读数据库、预算、
评分与报告 harness，不代表真实模型的语言泛化或质量。Week 3 已完成授权的 live 调试；本轮收尾只运行离线验证。

默认 fixture API 只识别两条内建演示问题，其他问题返回 unsupported；它不调用模型服务。
完整请求、状态、证据与事件重放示例见 [Agent API 本地使用指南](docs/agent-api.md)。

```bash
curl --fail http://127.0.0.1:8000/healthz
curl --fail -X POST http://127.0.0.1:8000/v1/analyses \
  -H 'Content-Type: application/json' \
  -d '{"query":"2026年6月GMV是多少？"}'
```

当前 live provider 为 DeepSeek，默认使用 `deepseek-v4-flash` 的非思考模式。配置 Key、执行一次不可重试的
裸基线以及根据结果确定第 2 周优先级的步骤见 [DeepSeek live 基线运行手册](docs/live-baseline-playbook.md)；
黄金问题的分层扩展原则见 [Golden Questions 优化策略](docs/golden-questions-strategy.md)。v1 的适配器根因见
[DeepSeek 裸基线 v1 分析](docs/reports/week-1-deepseek-live-analysis-2026-09-03.md)，正式 v2 结果、逐类根因和
第 2 周优先级见 [DeepSeek 裸基线 v2 分析](docs/reports/week-1-deepseek-live-v2-analysis-2026-09-03.md)。两轮
正式 JSON/Markdown 与逐题计量已脱敏归档在 [live 证据快照](docs/reports/evidence/README.md)。

第 2 周四类工具、SQL 策略和稳定错误契约见 [安全工具层指南](docs/safe-tools.md)；Week 2 live 的完整指标、
失败模式与第 3 周优先级见
[Week 2 DeepSeek live 分析](docs/reports/week-2-deepseek-live-analysis-2026-09-04.md)，Week 1 → Week 2 的
可比边界见 [对比报告](docs/reports/week-1-to-week-2-comparison-2026-09-04.md)。Week 1/2 三次正式 live JSON/Markdown
已归档在 [live 证据快照](docs/reports/evidence/README.md)。

## Database

本地 PostgreSQL、Alembic 迁移 `0001`–`0003`、最小权限角色和测试工作流已就绪。连接角色、
12 张业务表、可复制的开发命令及权限边界见 [数据库开发指南](docs/database.md)。

## 当前边界

- 本地开发基线包含 Docker Compose PostgreSQL、Alembic `0001`–`0003` 迁移、12 张业务表和固定
  synthetic dataset；full 数据集仅允许本地显式生成，绝不进入 CI。
- GitHub 远程仓库已配置；未经确认，不进行云端部署或公开发布。
- 未经确认，不创建云资源、不产生付费调用、不使用真实个人或企业数据。
- 2026-09-04 的 Week 2 live 单次授权已经使用并完成；任何后续 live 重跑仍须获得新的明确付费授权。
- Week 3 工程收尾包括 40 例 fixture 与已有 live 证据归档；保留两条已知路由偏差，详见交付记录。
- RunStore/EventStore 为内存存储，进程重启后任务丢失；当前按单进程本地 API 使用，尚无身份认证与公网部署验收。
- Compose 当前仅启动数据库；Streamlit、完整应用一键启动和演示留待第四周。

## License

[MIT](LICENSE)
