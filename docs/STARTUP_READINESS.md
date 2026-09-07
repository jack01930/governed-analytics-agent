# 项目启动就绪报告

> 最新同步日期：2026-09-07
> 检查机器：macOS 26.6 / Apple Silicon arm64
> 范围：只检查和准备本地开发环境，不创建云资源或公开远程仓库。

## 结论

第三周 Agent/API 核心工程已完成，收尾重跑离线门禁并通过本机 Uvicorn 的 HTTP/SSE 演示验证。
本轮没有新增模型付费调用。验证证据和两条已知路由问题见
[第三周交付记录](reports/week-3-closeout-2026-09-07.md)，API 使用方法见 [本地指南](agent-api.md)。

现有 `.env` 保持本地且被 Git 忽略；初始化时仅在文件不存在时复制模板，避免覆盖配置。
本机 `psql` 属于可选项，可以使用 PostgreSQL 容器内的客户端。

## 环境基线（版本与容量为 2026-09-04 历史快照）

| 项目 | 状态 | 说明 |
|---|---:|---|
| 项目规划 | 已就绪 | 已有完整任务契约、架构、评测和八周计划 |
| 本地 Git 仓库 | 已就绪 | 默认分支为 `main`，已连接 private `origin` |
| Python | 已就绪 | Python 3.12.14，路径 `/opt/homebrew/bin/python3.12` |
| 虚拟环境 | 已就绪 | 项目本地 `.venv`，不提交 Git |
| Python 依赖管理 | 已就绪 | `uv` 0.12.8，使用 `pyproject.toml` 和 `uv.lock` |
| Git | 已就绪 | Apple Git 2.50.1 |
| Git 作者信息 | 已就绪 | 本仓库使用 `jue` 和 GitHub noreply 邮箱，不修改全局配置 |
| GitHub CLI | 已就绪 | `gh` 2.97.0，已登录 |
| Docker | 已就绪 | Docker Desktop 4.89.0；Server/CLI 29.7.2；Compose v5.5.0 |
| 编译工具 | 已就绪 | Xcode Command Line Tools 可用 |
| 磁盘空间 | 已就绪 | 检查时约 244 GiB 可用 |
| 计划端口 | 按需检查 | 数据库 `5432`、API `8000`、未来界面 `8501`；占用情况以启动时为准 |
| 密钥隔离 | 已就绪 | `.env` 已被忽略，只提交 `.env.example` |
| Week 2 安全工具层 | 已就绪 | 4 类工具、70 例评测、SQL 策略、fixture 和一次 live 全流程已通过 |

## 第四周衔接

- 实现 Streamlit 及结果、证据展示，完成应用和数据库的完整启动流程。
- 改善澄清路由的时间窗口识别；固定回归样例验证变化。
- 当前状态存于内存，不承诺进程重启恢复；Checkpoint 和审批仍按第五周计划推进。
- 最终统一固定题集测评延期至候选完成或求职前，不阻塞第四周开发。

## 本地配置说明

### 1. 配置模型 API Key——已完成

```bash
test -f .env || cp .env.example .env
```

本机 `.env` 已配置 `MODEL_API_KEY`。检查脚本只判断是否存在，不输出密钥。任何真实密钥都不得写入
`.env.example`、文档、测试夹具或 Git 历史。

### 2. 本机 psql——可选

项目可以使用容器内的 `psql`，因此本机客户端不是硬要求。如需安装：

```bash
brew install libpq
brew link --force libpq
psql --version
```

`brew link --force` 会修改 Homebrew 链接；若不希望这样做，可直接使用 `$(brew --prefix libpq)/bin/psql`。

## GitHub 远程仓库

远程仓库使用 `jack01930/governed-analytics-agent`，初始可见性为 private。达到第 3～4 周可投递 MVP 且通过安全检查后，再由用户确认是否改为 public。

## 第 1 周启动门禁（历史记录）

开始第 1 周功能实现前：

- [x] 需求和验收标准冻结
- [x] 本地 Git 仓库建立
- [x] Python 3.12 虚拟环境建立
- [x] `.env` 和生成物已从 Git 排除
- [x] 可重复环境检查命令可用
- [x] Docker Compose v2 可用且 daemon 正常
- [x] Git 作者信息已配置
- [x] 使用 `uv` 管理 Python 依赖
- [x] 创建 `pyproject.toml` 和锁文件
- [x] 真实模型联调前配置 API Key
- [x] GitHub 远程仓库使用 private 可见性

运行以下命令可随时复查：

```bash
make doctor
```
