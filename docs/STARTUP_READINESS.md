# 项目启动就绪报告

> 检查日期：2026-09-01
> 检查机器：macOS 26.6 / Apple Silicon arm64
> 范围：只检查和准备本地开发环境，不创建云资源或公开远程仓库。

## 结论

项目仓库和本地开发环境已经具备开始第一周实现的条件。目前没有开工硬阻塞。

模型 API Key 在真实模型联调前补齐即可；本机 `psql` 属于可选项，不阻塞开发，因为可以使用 PostgreSQL 容器内的客户端。

## 已就绪

| 项目 | 状态 | 说明 |
|---|---:|---|
| 项目规划 | 已就绪 | 已有完整任务契约、架构、评测和八周计划 |
| 本地 Git 仓库 | 已就绪 | 默认分支为 `main`，尚无远程仓库 |
| Python | 已就绪 | Python 3.12.14，路径 `/opt/homebrew/bin/python3.12` |
| 虚拟环境 | 已就绪 | 项目本地 `.venv`，不提交 Git |
| Python 依赖管理 | 已就绪 | `uv` 0.12.8，使用 `pyproject.toml` 和 `uv.lock` |
| Git | 已就绪 | Apple Git 2.50.1 |
| Git 作者信息 | 已就绪 | 本仓库使用 `jue` 和 GitHub noreply 邮箱，不修改全局配置 |
| GitHub CLI | 已就绪 | `gh` 2.97.0，已登录 |
| Docker | 已就绪 | Docker Desktop 4.89.0；Server/CLI 29.7.2；Compose v5.5.0 |
| 编译工具 | 已就绪 | Xcode Command Line Tools 可用 |
| 磁盘空间 | 已就绪 | 检查时约 244 GiB 可用 |
| 计划端口 | 已就绪 | `5432`、`8000`、`8501` 均未占用 |
| 密钥隔离 | 已就绪 | `.env` 已被忽略，只提交 `.env.example` |

## 尚需处理

### 1. 配置模型 API Key——真实模型联调前必须

```bash
cp .env.example .env
```

然后只编辑本地 `.env` 中的 `MODEL_API_KEY`。检查脚本只判断是否存在，不输出密钥。任何真实密钥都不得写入 `.env.example`、文档、测试夹具或 Git 历史。

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

## 开工门禁

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
- [ ] 真实模型联调前配置 API Key
- [x] GitHub 远程仓库使用 private 可见性

运行以下命令可随时复查：

```bash
make doctor
```
