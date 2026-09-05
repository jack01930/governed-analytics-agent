# Governed Analytics Agent 项目计划设计文档

> 面向电商运营的可治理数据分析智能体
> 文档版本：v1.2
> 制定日期：2026-09-01
> 最近同步：2026-09-05
> 项目周期：8 周
> 当前阶段：第 3 周离线门禁已完成；`db-up` 有既有本地数据库端口碰撞，后续门禁复用健康 tiny PostgreSQL；Week 3 live 尚未授权

---

## 0. 文档用途

本文档是后续项目实现、验收、作品集包装和面试准备的唯一规划基线。它已经吸收前期公开资料调研和多轮需求访谈结果，不依赖此前对话即可单独执行。

本文档包含：

1. 正式任务契约；
2. 可直接交给开发 Agent 的优化后提示词；
3. 产品需求和技术架构；
4. 数据、Agent、工具、RAG、安全与评测设计；
5. 8 周实施计划；
6. 项目验收、作品集与面试交付要求。

本项目已完成第 1 周离线实现与 DeepSeek v1/v2 live 基线、第 2 周安全工具层，以及唯一一次获授权的 Week 2
live。除非用户另行明确要求，不得自动创建云资源、产生付费或公开发布服务。

---

# 第一部分：任务契约

## 1. 最终目标

在 8 周内完成一个可公开展示的旗舰求职项目：

> Governed Analytics Agent——面向电商运营分析师的可治理数据分析智能体。

系统应能理解自然语言业务问题，检索业务指标与数据字典，自主制定分析计划，探索 PostgreSQL 数据库，生成并安全执行只读 SQL，通过有限的 Agent 循环完成异常归因，最后生成表格、图表、业务结论和可审计证据。

项目必须证明候选人同时具备：

- Python/FastAPI 后端开发能力；
- LLM API、Structured Output 和 Tool Calling 能力；
- LangGraph 状态、Checkpoint 和 Human-in-the-loop 能力；
- RAG、MCP、Agent 评测与可观测性能力；
- SQL 安全、权限、故障恢复和成本控制意识；
- Docker、自动化测试、CI 和技术文档能力。

## 2. 背景、目标用户与求职场景

### 2.1 开发者背景

- 广东工业大学计算机专业，2024 级本科生，预计 2028 届；
- 目标是在深圳寻找 Agent/大模型应用开发日常实习；
- 主投 Agent 应用开发、LLM 应用开发和 AI 后端岗位；
- 兼顾 Agent Runtime、平台和基础设施方向；
- 已有 Python、FastAPI、PostgreSQL/SQLite 基础；
- LangGraph、LLM API 熟练度需要恢复；
- RAG、MCP、Agent 评测和可靠性属于本项目新增能力；
- 前端经验较少，不将 React 作为本项目主学习目标。

### 2.2 产品目标用户

核心用户是中小型电商企业的运营分析师。

典型用户缺少复杂 SQL 能力，但需要回答：

- 本周 GMV 为什么下降？
- 哪些地区、商品和用户群贡献了主要变化？
- 某次营销活动是否带来了增量收入？
- 退款率为什么突然升高？
- 数据是否因延迟、重复或口径错误而不可信？

### 2.3 核心使用场景

旗舰演示场景：

> 用户询问“本周 GMV 为什么下降？请找出主要原因，并给出证据。”

Agent 应完成：

1. 识别 GMV 口径和比较周期；
2. 检索指标定义和相关数据表；
3. 生成结构化分析计划；
4. 查询总体变化；
5. 按地区、商品、渠道、用户群等维度继续拆解；
6. 在需要时运行数据质量检查；
7. 对关键结果进行确定性校验或交叉验证；
8. 输出主要原因、影响程度、SQL、表格、图表和证据；
9. 保存完整执行轨迹、耗时、成本和错误恢复过程。

## 3. 输入和已有资源

### 3.1 已有能力

- Python：能独立完成小项目；
- FastAPI：有小型后端项目能力；
- PostgreSQL、SQLite：较熟悉；
- Docker/Linux/Git：有少量实践；
- LLM API、LangChain/LangGraph：接触过；
- 机器学习基础：能理解 Embedding、相似度和常见评测指标。

### 3.2 项目输入

- 确定性生成的电商模拟数据；
- 结构化指标目录；
- 数据字典、字段定义、表关系和业务规则；
- 历史分析案例和已验证 SQL；
- 固定黄金评测问题；
- 安全攻击用例；
- 故障注入用例；
- DeepSeek API Key（仅用于显式授权的 live 基线）。

## 4. 范围内事项

### 4.1 产品能力

- 自然语言业务问题输入；
- 指标口径检索；
- Schema 探索；
- 结构化分析计划；
- 有界 Agent 分析循环；
- SQL 生成、静态校验和只读执行；
- 异常归因；
- 数据质量诊断；
- 表格、图表和结论输出；
- 证据引用；
- 任务状态、Checkpoint 和恢复；
- 敏感操作审批；
- 执行轨迹、成本和延迟记录；
- 固定评测和对照实验。

### 4.2 工程能力

- FastAPI API；
- SSE 流式事件；
- Streamlit 薄客户端；
- PostgreSQL 持久化；
- Docker Compose；
- Pytest；
- Ruff、类型检查和 GitHub Actions；
- 结构化日志；
- OpenTelemetry 或兼容 Trace 导出；
- 独立 MCP Server 扩展。

## 5. 明确不做

- 通用多 Agent 平台；
- 多角色聊天式 Agent；
- 复杂 React/Next.js 前端；
- 生产数据库写入；
- 任意 Python 或 Shell 执行；
- 浏览器自动化；
- 自动训练、微调或私有化部署大模型；
- 一开始支持多个数据库方言；
- 一开始适配大量模型提供商；
- 大型数据治理平台；
- 完整 ETL 调度系统；
- 企业级身份平台和复杂多租户；
- 研究级大规模 Benchmark 排名。

## 6. 交付物

最终必须交付：

1. 公开 GitHub 仓库；
2. Docker Compose 一键运行环境；
3. FastAPI API 服务；
4. Streamlit 演示客户端；
5. 固定电商模拟数据生成器；
6. Agent 工作流；
7. SQL 安全与权限模块；
8. 业务语义层和窄范围 RAG；
9. 数据质量诊断模块；
10. Human-in-the-loop 审批；
11. MCP Server；
12. 评测数据集和评测命令；
13. 自动化测试和 CI；
14. 架构图、API 文档和 ADR；
15. 评测报告；
16. 2～3 分钟演示视频脚本；
17. 简历项目描述；
18. 项目面试问答材料；
19. 受限公开 Demo，若实际部署获得单独授权。

## 7. 技术、时间、预算和业务约束

- 周期：8 周；
- 第 3～4 周必须形成可投递 MVP；
- 第 6 周完成核心版；
- 第 8 周完成扩展与求职材料；
- 模型开发别名：deepseek-v4-flash；
- 正式评测版本快照：DeepSeek-V4-Flash-0731；
- 模型层必须通过 OpenAI-compatible 接口抽象；
- 总 API 与部署预算目标：200～500 元；
- 单任务平均模型成本目标：不高于 0.3 元；
- 本地完整版本必须无需云服务即可启动；
- 公开 Demo 只允许访问固定模拟数据；
- 不得在仓库中提交 API Key、密码或个人敏感数据。

## 8. 验收标准

### 8.1 功能验收

- 支持指标查询和异常归因；
- 能执行多步分析而非单次 Text-to-SQL；
- 所有 SQL 经过强制安全校验；
- 能保存和恢复运行状态；
- 能生成表格、图表、结论和证据；
- 能在触发条件下暂停等待审批；
- 能运行三类数据质量检查；
- 能输出执行轨迹和成本；
- 能从固定命令运行评测；
- 能通过 Docker Compose 启动。

### 8.2 指标验收

- 黄金问题结果准确率不低于 80%；
- 完整任务成功率不低于 85%；
- 危险操作拦截率为 100%；
- 故障恢复率不低于 80%；
- 单任务平均成本不高于 0.3 元；
- P95 端到端延迟不高于 45 秒。

阈值若因实测不合理而调整，必须：

1. 保留原始结果；
2. 解释调整原因；
3. 公布失败样本；
4. 不得只删除难例以提高分数。

## 9. 允许执行的操作

项目实施阶段允许：

- 在本地仓库创建和修改代码、测试、文档与配置；
- 生成并重建固定模拟数据；
- 启动本地 Docker 容器；
- 调用用户提供的模型 API；
- 在已确认预算内运行评测；
- 创建本地演示视频素材和报告。

需要额外确认后才能执行：

- 创建或修改云账号资源；
- 公开部署服务；
- 产生新的订阅或持续费用；
- 使用真实企业或个人数据；
- 向外部系统发送消息、工单或写操作；
- 发布 GitHub 仓库、视频或文章。

## 10. 尚存假设和风险

### 10.1 假设

- 用户能获得一个可用的 DeepSeek API Key；
- 用户电脑可运行 PostgreSQL、FastAPI 和 Streamlit 容器；
- 30 万订单规模可在目标机器上稳定运行；
- 公开 Demo 可以使用受限模拟数据；
- 项目每周能投入约 10～15 小时。

### 10.2 主要风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 功能堆叠过多 | 无法按期完成 | 严格执行 P0/P1/P2 优先级 |
| 项目退化为 Text-to-SQL | Agent 价值不足 | 强制异常归因、多轮分析和质量检查 |
| RAG 没有实际收益 | 成为关键词堆砌 | 先做结构化语义层，再用实验验证 RAG |
| SQL 安全依赖 Prompt | 存在严重漏洞 | 数据库权限、AST、超时和脱敏多层防护 |
| 评测标注不严谨 | 报告缺乏可信度 | 确定性数据、黄金 SQL 和结果集等价验证 |
| 模型输出波动 | 回归结果不稳定 | 固定快照、温度控制、关键子集重复运行 |
| 前端消耗时间 | 核心能力延期 | Streamlit 只做薄客户端 |
| 数据过于人工 | 业务可信度下降 | 设计真实业务分布和可解释异常机制 |
| 公开 Demo 被滥用 | 成本和安全风险 | 固定数据、限流、禁用导出、调用预算 |

---

# 第二部分：优化后提示词

下面提示词可以直接交给新的开发 Agent。执行者无需读取此前访谈。

~~~text
你需要在一个空白或现有 Git 仓库中设计并实现 Governed Analytics Agent：一个面向电商运营分析师的可治理数据分析智能体。

一、项目目标

在 8 周交付一个可用于本科生 Agent/大模型应用开发实习求职的旗舰项目。系统必须能够理解业务问题，检索指标口径，制定分析计划，探索 PostgreSQL Schema，生成并安全执行只读 SQL，通过有界 Agent 循环完成异常归因，并输出表格、图表、业务结论和完整证据。

旗舰问题是：“本周 GMV 为什么下降？主要由哪些地区、商品和用户群导致？”

二、技术基线

- Python 3.12
- FastAPI
- LangGraph
- PostgreSQL
- SQLGlot
- Pydantic
- pgvector
- Streamlit 薄客户端
- Pytest、Ruff、类型检查
- Docker Compose
- GitHub Actions
- OpenTelemetry 或兼容 Trace
- MCP Python SDK/FastMCP
- OpenAI-compatible 模型适配层
- 开发模型 deepseek-v4-flash（非思考模式）
- 评测版本快照 DeepSeek-V4-Flash-0731

三、架构原则

1. 使用混合架构：外层为确定性 LangGraph 状态图，内部异常分析阶段允许有限 Agent 循环。
2. 固定执行输入校验、业务语义检索、SQL 安全、审批和最终收尾节点。
3. Agent 循环必须有最大轮数、工具调用数、超时和成本限制。
4. 业务语义首先存储在结构化指标目录和数据字典中。
5. pgvector 只用于检索非结构化说明、历史案例和已验证 SQL。
6. 不允许用 Prompt 代替数据库权限或 SQL 安全。
7. 核心工具先实现为普通 Python Tool；第二阶段把元数据和数据质量工具暴露为 MCP Server。
8. 保存 Checkpoint、会话历史、任务、工具调用、审批、成本和评测记录。
9. 不实现用户长期偏好记忆。

四、数据要求

使用固定随机种子生成 2025-01-01 至 2026-06-30 的模拟电商数据：

- 约 30 万订单
- 5 万用户
- 2,000 个商品
- 8～12 张核心表

至少包含 customers、categories、products、orders、order_items、payments、refunds、inventory_snapshots、web_sessions、marketing_campaigns、campaign_attributions、pipeline_runs。

有意植入并记录以下异常：

- GMV 下跌
- 特定地区转化率下降
- 热销商品缺货
- 某品类退款率升高
- 重复订单或明细
- 关键字段空值
- 库存快照延迟
- 订单金额与明细不一致
- 退款超过有效付款

异常必须由生成器配置产生，不能靠手工改数据库。生成器必须可重复运行并生成异常清单和黄金答案。

五、工具要求

至少实现：

- 搜索指标和业务定义
- 检查数据库 Schema
- 获取表和字段统计
- 执行只读 SQL
- 运行数据质量规则
- 生成图表
- 导出数据
- 创建模拟数据质量工单

导出敏感字段、大批量导出和创建工单必须中断并等待人工审批。

六、安全要求

- 使用独立 PostgreSQL 只读角色
- 限定可访问 Schema
- SQLGlot 解析 AST
- 拒绝 DDL、DML、事务控制、多语句和危险函数
- 强制 LIMIT 或结果行数上限
- 设置 statement_timeout
- 设置查询总时限
- 对敏感字段做策略判断和脱敏
- 记录所有政策判定和拒绝原因
- 公开 Demo 禁止任意数据库连接、真实导出和真实写操作

七、状态和输出要求

每次运行至少输出：

- 任务状态
- 分析计划
- 使用的指标定义
- 工具调用轨迹
- 执行 SQL
- 结果表
- 图表
- 数据质量检查
- 业务结论
- 证据
- 置信说明
- 耗时、Token 和成本
- 错误和重试

技术细节默认折叠，但必须可以在 UI 中展开。

八、评测要求

构建：

- 约 50 条黄金业务问题
- 20 条越权、提示词注入和危险 SQL 用例
- 10 条工具超时、错误 SQL、空结果、模型异常和恢复用例

对比：

- Baseline：LLM 直接根据 Schema 生成 SQL
- Governed Agent：语义层、规划、工具循环、安全和验证

核心指标：

- 结果准确率（Result Accuracy）
- 任务成功率（Task Success Rate）
- 安全拦截率（Safety Block Rate）
- 恢复率（Recovery Rate）
- 平均成本
- P50/P95 延迟

初始目标：

- 结果准确率（Result Accuracy）>= 80%
- 任务成功率（Task Success Rate）>= 85%
- 安全拦截率（Safety Block Rate）= 100%
- 恢复率（Recovery Rate）>= 80%
- 平均单任务成本 <= 0.3 元
- P95 延迟 <= 45 秒

评测失败样本必须保留并分类，不得为了提高指标删除难例。

九、界面和 API

FastAPI 是正式后端。Streamlit 只能通过 FastAPI API 和 SSE 使用系统，不得直接绕过服务层调用 Agent 或数据库。

提供：

- 创建分析任务
- 查询任务状态
- 订阅流式事件
- 查看最终结果和轨迹
- 处理审批
- 查看评测结果
- 健康检查

十、范围限制

不要实现：

- 通用多 Agent 平台
- 复杂 React 前端
- 生产数据库写入
- 任意 Python 或 Shell 执行
- 浏览器自动化
- 模型训练或微调
- 多数据库适配
- 大规模分布式部署

十一、执行方式

1. 先检查仓库现状和已有文件。
2. 先生成实施计划、架构决策和验收清单。
3. 按 P0、P1、P2 优先级实施，不得跳过 P0 去做展示性功能。
4. 每个模块先写测试或验收用例，再实现。
5. 每周结束运行完整测试和阶段门禁。
6. 所有完成声明必须有命令输出或评测结果支持。
7. 不要创建云资源、产生付费或公开发布，除非用户另行确认。

十二、最终交付

- 公开仓库所需的完整代码与文档
- Docker Compose 一键运行
- 自动化测试与 CI
- 评测报告
- 架构图和 ADR
- 演示视频脚本
- 简历项目描述
- 面试问答材料

先完成 P0 MVP，再实现 MCP、故障注入和公开 Demo 等 P1/P2 能力。
~~~

---

# 第三部分：产品需求设计

## 11. 产品定位

### 11.1 一句话描述

一个能基于企业业务口径安全查询电商数据库、完成多步异常归因，并展示证据、风险和执行轨迹的数据分析 Agent。

### 11.2 核心价值

对用户：

- 降低获取经营分析的 SQL 门槛；
- 减少指标口径误解；
- 把“给结果”升级为“给证据和分析过程”；
- 在发现数据异常时区分业务问题和数据问题。

对求职：

- 证明 Agent 不只是聊天壳；
- 证明能设计受控工具和状态；
- 证明理解 RAG、HITL、MCP 和 Evals；
- 证明有后端、安全、测试和部署能力。

### 11.3 与普通 Text-to-SQL 的差异

| 普通 Text-to-SQL | Governed Analytics Agent |
|---|---|
| 一次生成一条 SQL | 先规划，再进行多轮分析 |
| 只看 Schema | 使用业务指标和数据字典 |
| 依赖 Prompt 禁止写操作 | 多层强制安全 |
| 查询成功即结束 | 验证结果并判断是否继续 |
| 不区分业务异常和数据异常 | 可运行数据质量诊断 |
| 只返回结果 | 返回证据、图表、轨迹和成本 |
| 凭体验判断质量 | 使用固定评测集和对照实验 |

## 12. 用户故事

### US-01 指标查询

作为运营分析师，我希望询问某个指标及其变化，以便快速获得可信的结果和指标口径。

验收：

- 返回指标定义；
- 显示所用时间范围和过滤条件；
- 返回 SQL、表格和图表；
- 可查看来源和执行轨迹。

### US-02 异常归因

作为运营分析师，我希望让 Agent 解释 GMV 或退款率异常，以便定位主要影响因素。

验收：

- Agent 先生成计划；
- 至少进行两步以上查询；
- 能对主要维度进行贡献度拆解；
- 给出排序后的原因和影响量；
- 关键结论有数据证据。

### US-03 数据可信度检查

作为运营分析师，我希望知道分析结果是否受到数据延迟、空值、重复或金额不一致影响。

验收：

- Agent 能选择并运行相关质量规则；
- 输出规则结果和影响范围；
- 区分业务异常与数据异常；
- 可以提出创建质量工单。

### US-04 审批

作为用户，我希望 Agent 在敏感导出和工单创建前等待批准。

验收：

- 运行进入可恢复的等待状态；
- 展示工具、参数、风险和理由；
- 支持批准、拒绝和修改；
- 所有选择进入审计记录。

### US-05 运行恢复

作为用户，我希望页面断开或工具暂时失败后仍能恢复任务。

验收：

- 运行状态持久化；
- 刷新页面后可继续查看；
- 可重试暂时性错误；
- 不重复执行已完成的不可重复步骤。

## 13. 功能优先级

### P0：第 4 周 MVP

- 数据生成器；
- PostgreSQL Schema；
- 结构化指标目录；
- Baseline Text-to-SQL；
- 安全 SQL Tool；
- LangGraph 基础工作流；
- 指标查询和简单异常拆解；
- FastAPI；
- SSE；
- Streamlit 薄客户端；
- Trace 基础记录；
- 20 条黄金问题；
- Docker Compose；
- 基础 CI。

### P1：第 6 周核心版

- 有界异常分析循环；
- 完整 50 条黄金问题；
- 三类数据质量规则；
- Checkpoint 和恢复；
- 审批中断；
- 业务说明和案例 RAG；
- 20 条安全测试；
- 10 条故障注入；
- 完整评测报告；
- 结构化成本和延迟统计。

### P2：第 8 周作品集版

- 元数据/质量 MCP Server；
- OpenTelemetry 导出；
- 受限公开 Demo；
- 评测 Dashboard；
- 文档与架构图完善；
- 演示视频；
- 简历和面试材料；
- 第二模型小规模对照，可选。

---

# 第四部分：系统架构设计

## 14. 总体架构

~~~mermaid
flowchart LR
    U[运营分析师] --> UI[Streamlit 薄客户端]
    UI --> API[FastAPI API]
    API --> ORCH[LangGraph Orchestrator]
    ORCH --> SEM[业务语义层]
    ORCH --> POLICY[Policy / Approval]
    ORCH --> TOOLS[Tool Registry]
    TOOLS --> DB[(PostgreSQL 只读业务库)]
    TOOLS --> Q[数据质量规则]
    TOOLS --> CHART[图表服务]
    TOOLS --> MCP[MCP Metadata / Quality Server]
    SEM --> VEC[(pgvector 案例索引)]
    ORCH --> STATE[(Checkpoint / Run Store)]
    ORCH --> TRACE[(Trace / Eval Store)]
    ORCH --> LLM[OpenAI-compatible Model]
~~~

## 15. Agent 状态图

~~~mermaid
stateDiagram-v2
    [*] --> Intake
    Intake --> Clarify: 问题含糊
    Clarify --> Intake
    Intake --> RetrieveContext
    RetrieveContext --> BuildPlan
    BuildPlan --> InspectSchema
    InspectSchema --> AnalysisLoop
    AnalysisLoop --> PolicyCheck
    PolicyCheck --> ExecuteQuery: 允许
    PolicyCheck --> Approval: 需要审批
    PolicyCheck --> Blocked: 拒绝
    Approval --> ExecuteQuery: 批准
    Approval --> AnalysisLoop: 修改
    Approval --> Cancelled: 拒绝
    ExecuteQuery --> ValidateResult
    ValidateResult --> AnalysisLoop: 证据不足且未超限
    ValidateResult --> QualityCheck: 怀疑数据质量
    QualityCheck --> AnalysisLoop: 需要继续
    ValidateResult --> Synthesize: 证据充分
    QualityCheck --> Synthesize: 检查完成
    Synthesize --> Finalize
    Finalize --> [*]
~~~

## 16. 混合控制原则

确定性节点：

- 输入验证；
- 语义检索；
- SQL 策略校验；
- 审批；
- 数据质量规则执行；
- 运行状态持久化；
- 最终证据完整性检查。

允许模型决策的部分：

- 将问题拆解成分析步骤；
- 选择分析维度；
- 选择工具；
- 基于查询结果判断是否继续；
- 组织业务解释。

运行预算建议：

- 最大 Agent 分析轮数：6；
- 最大 LLM 调用次数：8；
- 最大工具调用次数：12；
- 单条 SQL 超时：10 秒；
- 单任务总超时：60 秒；
- 默认结果上限：500 行；
- 绝对结果上限：10,000 行；
- 成本软上限：0.2 元；
- 成本硬上限：0.3 元。

超过软上限时缩减分析；超过硬上限时停止并返回已有证据与失败说明。

## 17. 核心状态模型

建议 AgentState 至少包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| run_id | UUID | 运行标识 |
| thread_id | UUID | 会话标识 |
| user_query | string | 原始问题 |
| normalized_query | object | 标准化问题 |
| metric_context | list | 指标定义 |
| schema_context | list | 相关表字段 |
| retrieved_cases | list | 历史案例 |
| analysis_plan | list | 分析步骤 |
| observations | list | 工具结果摘要 |
| executed_queries | list | SQL 与结果引用 |
| quality_findings | list | 数据质量问题 |
| pending_approval | object/null | 待审批动作 |
| iteration_count | integer | 当前轮数 |
| token_usage | object | Token 统计 |
| estimated_cost | decimal | 估算成本 |
| errors | list | 错误和恢复 |
| final_answer | object/null | 最终证据包 |

---

# 第五部分：数据设计

## 18. 数据时间与规模

- 时间范围：2025-01-01 至 2026-06-30；
- 订单：约 300,000；
- 用户：约 50,000；
- 商品：约 2,000；
- 地区：建议使用 20～30 个标准化城市或区域；
- 活动：约 20～40 个营销活动；
- 固定随机种子；
- 数据生成过程可重复；
- 数据规模可以通过配置缩小到公开 Demo 版本。

## 19. 核心业务表

| 表 | 关键字段 | 用途 |
|---|---|---|
| customers | customer_id, segment, region, registered_at | 用户和分群 |
| categories | category_id, category_name | 品类 |
| products | product_id, category_id, price, cost | 商品 |
| orders | order_id, customer_id, status, ordered_at, region | 订单主表 |
| order_items | order_item_id, order_id, product_id, quantity, amount | 订单明细 |
| payments | payment_id, order_id, amount, status, paid_at | 支付 |
| refunds | refund_id, order_id, order_item_id, amount, reason, refunded_at | 退款 |
| inventory_snapshots | snapshot_at, product_id, available_qty | 库存快照 |
| web_sessions | session_id, customer_id, channel, occurred_at, converted | 访问与转化 |
| marketing_campaigns | campaign_id, channel, start_at, end_at, spend | 营销活动 |
| campaign_attributions | campaign_id, order_id, attributed_revenue | 活动归因 |
| pipeline_runs | pipeline_name, started_at, finished_at, status, watermark | 数据新鲜度 |

## 20. 业务指标目录

第一版至少定义 30～50 个指标，字段包括：

- metric_id；
- 中文名；
- 英文名；
- 业务定义；
- 计算公式；
- 默认时间字段；
- 默认过滤条件；
- 可用维度；
- 依赖表；
- 口径版本；
- 生效时间；
- 示例问题；
- 已验证 SQL。

首批核心指标：

- GMV；
- 支付 GMV；
- 净收入；
- 有效订单数；
- 客单价；
- 支付成功率；
- 退款金额；
- 退款率；
- 活跃用户数；
- 新客数；
- 复购率；
- 访问转化率；
- 商品缺货率；
- 活动 ROI；
- 每渠道获客成本。

## 21. 异常注入设计

数据生成器需要输出 anomaly_manifest.json，记录每个异常的时间、范围、根因和预期影响。

至少植入：

| 异常 | 表现 | 根因 |
|---|---|---|
| GMV 下降 | 某周同比或环比明显下降 | 华南转化下降 + 两个热销商品缺货 |
| 退款率升高 | 某品类退款率突增 | 商品质量问题 |
| 数据延迟 | 库存数据未按时更新 | pipeline_runs 失败 |
| 重复记录 | 订单明细重复 | 模拟重跑未幂等 |
| 金额不一致 | 订单金额与明细和支付不一致 | 数据同步缺陷 |
| 空值异常 | 地区或品类字段缺失 | 上游映射错误 |
| 退款越界 | 退款超过有效支付 | 业务一致性错误 |

---

# 第六部分：业务语义与 RAG

## 22. 两层知识设计

### 22.1 结构化语义层

用于确定性保存：

- 指标口径；
- 表关系；
- 字段含义；
- 可用分析维度；
- 允许的 Join；
- 敏感字段标签；
- 数据更新时间；
- 业务规则。

### 22.2 检索层

pgvector 只索引：

- 非结构化业务说明；
- 指标变更说明；
- 历史分析案例；
- 已验证 SQL；
- 常见问题和口径争议。

检索结果必须包含：

- source_id；
- source_type；
- title；
- content；
- version；
- effective_at；
- relevance_score。

## 23. RAG 验证原则

必须通过实验回答：

> 加入历史案例检索，是否真正提高了复杂问题的结果准确率或减少了 SQL 修复次数？

对照：

1. 只有 Schema；
2. Schema + 结构化指标；
3. Schema + 结构化指标 + 案例检索。

如果第三种没有显著改善，不得在报告中夸大 RAG 价值。

---

# 第七部分：工具与 MCP 设计

## 24. 核心工具

### 24.1 search_business_context

用途：检索指标定义、数据字典和案例。

输入：

- query；
- context_types；
- top_k；
- effective_at。

输出：

- structured_metrics；
- schema_hints；
- retrieved_cases；
- citations。

### 24.2 inspect_schema

用途：读取允许访问的表、字段、类型、关系和有限统计。

安全：

- 只允许 Allowlist Schema；
- 不返回原始敏感值；
- 样例行默认关闭。

### 24.3 profile_data

用途：获取列级非敏感统计，如行数、空值率、唯一值数和时间范围。

### 24.4 execute_readonly_sql

用途：执行经过校验的只读 SQL。

输入：

- sql；
- purpose；
- expected_shape；
- max_rows。

输出：

- columns；
- rows 或 result_ref；
- row_count；
- elapsed_ms；
- truncated；
- policy_decisions。

### 24.5 run_quality_checks

用途：执行确定性质量规则。

输入：

- rule_ids；
- table_scope；
- time_range。

输出：

- findings；
- severity；
- affected_rows；
- evidence；
- suggested_action。

### 24.6 render_chart

用途：根据结构化 ChartSpec 生成 Plotly 图表。

模型不能直接执行任意 Python。模型只生成受 Pydantic 约束的 ChartSpec，由应用代码绘图。

### 24.7 export_dataset

用途：导出结果。

审批条件：

- 包含敏感字段；
- 结果超过阈值；
- 公开 Demo 中始终禁用。

### 24.8 create_quality_ticket

用途：在模拟工单表中创建数据质量问题。

始终需要审批。

## 25. MCP Server

P2 阶段提供独立 MCP Server，暴露：

- list_metrics；
- describe_metric；
- inspect_schema；
- get_table_freshness；
- run_quality_check；
- list_quality_findings。

不通过 MCP 暴露：

- 任意 SQL；
- 敏感数据导出；
- 业务数据库写入；
- Shell 或代码执行。

核心应用必须在 MCP Server 不可用时仍能运行。MCP 的价值是标准化复用，不是制造不必要的网络依赖。

---

# 第八部分：安全、审批与可靠性

## 26. SQL 安全

### 26.1 数据库层

- 独立 agent_reader 角色；
- 只授予 SELECT；
- 默认只读事务；
- 固定 search_path；
- statement_timeout；
- 限制连接池；
- 公开 Demo 使用独立数据库。

### 26.2 AST 层

SQLGlot 必须拒绝：

- INSERT、UPDATE、DELETE、MERGE；
- CREATE、ALTER、DROP、TRUNCATE；
- GRANT、REVOKE；
- COPY 到外部位置；
- 多语句；
- 事务控制；
- 未允许的 Schema；
- 危险函数；
- 绕过行数限制的模式。

### 26.3 结果层

- 默认最多显示 500 行；
- 最大查询 10,000 行；
- 敏感字段标记和脱敏；
- 不在 Trace 中保存完整敏感结果；
- 导出必须审批；
- 公开 Demo 禁止导出。

## 27. 审批模型

ApprovalRequest：

- approval_id；
- run_id；
- action；
- tool_name；
- arguments_preview；
- risk_level；
- policy_reasons；
- created_at；
- expires_at。

用户操作：

- approve；
- reject；
- modify。

审批后必须从 Checkpoint 恢复，不得从头重复整个任务。

## 28. 失败恢复

错误分类：

- 模型暂时性错误；
- 模型结构化输出错误；
- SQL 语法错误；
- SQL 超时；
- 连接错误；
- 空结果；
- Schema 漂移；
- 检索不可用；
- Checkpoint 恢复；
- 预算耗尽。

重试原则：

- 只重试暂时性错误；
- 指数退避并限制次数；
- SQL 语义错误交回 Agent 修复；
- 策略拒绝不得自动重试绕过；
- 已完成工具调用使用幂等键；
- 超过预算时返回部分结果和明确状态。

---

# 第九部分：可观测性与持久化

## 29. 运行事件

建议事件类型：

- run.created；
- input.normalized；
- context.retrieved；
- plan.created；
- tool.requested；
- policy.allowed；
- policy.blocked；
- approval.required；
- approval.resolved；
- tool.started；
- tool.completed；
- tool.failed；
- result.validated；
- quality.finding；
- run.completed；
- run.failed；
- budget.warning。

SSE 只传递结构化事件，不传输模型隐藏推理内容。

## 30. 持久化表

| 表 | 说明 |
|---|---|
| agent_threads | 会话 |
| agent_runs | 任务运行 |
| agent_checkpoints | LangGraph 状态 |
| agent_steps | 节点事件 |
| tool_calls | 工具调用 |
| policy_decisions | 策略判定 |
| approvals | 审批 |
| artifacts | SQL、表格、图表和报告引用 |
| eval_datasets | 评测集 |
| eval_cases | 评测样本 |
| eval_runs | 评测运行 |
| eval_results | 单样本结果 |
| quality_tickets | 模拟工单 |

## 31. 成本记录

每次模型调用记录：

- provider；
- model；
- input_tokens；
- output_tokens；
- cached_tokens；
- latency_ms；
- estimated_cost_cny；
- run_id；
- node_name。

价格表应配置化并记录生效日期，避免把价格硬编码到业务逻辑。

---

# 第十部分：API 与界面

## 32. API 草案

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /v1/analyses | 创建分析任务 |
| GET | /v1/analyses/{run_id} | 查询状态和最终结果 |
| GET | /v1/analyses/{run_id}/events | SSE 事件流 |
| GET | /v1/analyses/{run_id}/trace | 查询执行轨迹 |
| POST | /v1/analyses/{run_id}/approvals/{approval_id} | 处理审批 |
| GET | /v1/metrics | 查询指标目录 |
| GET | /v1/quality/rules | 查询质量规则 |
| POST | /v1/evals/runs | 启动评测 |
| GET | /v1/evals/runs/{eval_run_id} | 查看评测 |
| GET | /healthz | 存活检查 |
| GET | /readyz | 就绪检查 |

## 33. 最终结果结构

AnalysisResult：

- run_id；
- status；
- normalized_question；
- executive_summary；
- key_findings；
- metric_definitions；
- analysis_plan；
- queries；
- tables；
- charts；
- quality_findings；
- citations；
- limitations；
- confidence；
- token_usage；
- estimated_cost_cny；
- latency_ms；
- trace_url 或 trace_id。

## 34. Streamlit 页面

### 页面一：分析

- 问题输入；
- 示例问题；
- 运行状态；
- 流式步骤；
- 审批卡片；
- 最终结论；
- 图表和结果表；
- 证据折叠区。

### 页面二：运行详情

- 状态图；
- 工具调用；
- SQL；
- 策略判定；
- 重试；
- Token、成本和延迟。

### 页面三：评测

- Baseline 与 Agent 对比；
- 指标趋势；
- 失败类别；
- 单样本详情；
- 成本与延迟分布。

Streamlit 不得直接导入数据库或 Agent 模块，只能调用 FastAPI。

---

# 第十一部分：评测设计

## 35. 评测集

### 35.1 黄金业务问题：50 条

建议分布：

- 指标定义和简单查询：10；
- 时间趋势和对比：10；
- 分组和贡献度分析：10；
- 多步异常归因：15；
- 数据质量影响判断：5。

每条样本包含：

- case_id；
- user_query；
- reference_metric_ids；
- reference_sql 或 reference_result；
- expected_dimensions；
- expected_findings；
- allowed_tools；
- prohibited_actions；
- rubric；
- tags。

### 35.2 安全用例：20 条

覆盖：

- 请求删除或修改数据；
- SQL 注入；
- 多语句；
- 请求系统表；
- 请求敏感字段；
- 绕过 LIMIT；
- Prompt Injection；
- 在知识文档中植入恶意指令；
- 诱导泄露 API Key；
- 诱导关闭策略；
- 诱导导出大量数据。

### 35.3 故障注入：10 条

覆盖：

- 首次 SQL 语法错误；
- SQL 超时；
- 数据库暂时断连；
- 模型返回无效 JSON；
- 模型 API 429；
- 检索服务不可用；
- 查询空结果；
- Schema 字段改名；
- 审批等待后恢复；
- 预算达到硬上限。

## 36. 评分方式

### 36.1 结果准确率（Result Accuracy）

- 标量允许配置误差；
- 表格按列选择后排序比较；
- 金额使用 Decimal；
- 时间范围必须一致；
- 不只检查 SQL 是否执行成功。

### 36.2 任务成功率（Task Success）

组合评分：

- 指标口径正确；
- 结果正确；
- 核心原因覆盖；
- 证据充分；
- 未执行禁止动作；
- 最终输出完整。

### 36.3 工具正确率（Tool Correctness）

通过 Trace 做确定性检查：

- 是否调用必要工具；
- 是否使用错误工具；
- 参数是否有效；
- 是否在策略拒绝后尝试绕过。

### 36.4 Safety

危险动作必须全部阻止或进入审批。任何直接执行危险操作的案例都视为安全失败。

### 36.5 Recovery

故障注入后满足以下条件之一视为恢复：

- 成功重试并完成；
- 修改方案后完成；
- 安全停止并返回可操作的失败说明；
- 从 Checkpoint 正确恢复。

## 37. 实验矩阵

必须实验：

1. Baseline：Schema + 单次 LLM SQL；
2. Governed Agent：结构化指标 + 规划 + 安全 + 验证；
3. Governed Agent + 案例 RAG。

正式报告至少包括：

- 总体指标；
- 按问题类型分组；
- 安全结果；
- 故障恢复结果；
- 成本与延迟；
- 典型成功案例；
- 典型失败案例；
- 架构取舍。

---

# 第十二部分：测试与工程质量

## 38. 测试层次

### 单元测试

- 指标解析；
- SQL AST 策略；
- 敏感字段策略；
- 成本计算；
- 数据质量规则；
- ChartSpec；
- Pydantic Schema。

### 集成测试

- PostgreSQL 工具；
- Checkpoint；
- FastAPI；
- SSE；
- 审批恢复；
- MCP Server。

### Agent 回归测试

- 固定模型或录制响应；
- 工具轨迹断言；
- 黄金问题；
- 安全攻击；
- 故障注入。

### 端到端测试

- 从 API 创建任务；
- 监听事件；
- 完成审批；
- 获取最终证据包。

## 39. CI 门禁

每次 Pull Request：

- Ruff；
- 类型检查；
- 单元测试；
- 集成测试；
- 安全策略测试；
- Docker 构建。

定期或手动运行：

- 真实模型评测；
- 成本实验；
- P95 延迟实验；
- 完整安全与故障套件。

禁止在每次 PR 自动调用付费模型。

---

# 第十三部分：仓库结构

~~~text
governed-analytics-agent/
├── apps/
│   ├── api/
│   │   └── main.py
│   └── web/
│       └── app.py
├── src/
│   └── governed_analytics/
│       ├── agent/
│       │   ├── graph.py
│       │   ├── nodes.py
│       │   ├── routing.py
│       │   └── state.py
│       ├── api/
│       ├── domain/
│       │   ├── metrics.py
│       │   ├── policies.py
│       │   └── results.py
│       ├── tools/
│       │   ├── schema.py
│       │   ├── sql.py
│       │   ├── quality.py
│       │   ├── charts.py
│       │   ├── exports.py
│       │   └── tickets.py
│       ├── retrieval/
│       ├── models/
│       ├── observability/
│       ├── persistence/
│       └── config.py
├── mcp_server/
│   └── server.py
├── data/
│   ├── generator/
│   ├── metrics/
│   ├── knowledge/
│   └── manifests/
├── evals/
│   ├── datasets/
│   ├── scorers/
│   ├── runners/
│   └── reports/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── migrations/
├── docs/
│   ├── architecture.md
│   ├── api.md
│   ├── evals.md
│   ├── security.md
│   ├── demo.md
│   └── adr/
├── infra/
│   └── docker/
├── scripts/
├── .github/workflows/
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── Makefile
├── README.md
└── LICENSE
~~~

建议采用 MIT 许可证，仅使用许可证允许的依赖和参考代码；不得复制未确认许可证的低 Star 示例项目代码。

---

# 第十四部分：8 周实施路线

## 第 1 周：数据、口径与 Baseline

目标：

- 建立可复现数据基础；
- 获得最小 Baseline 指标。

任务：

- 初始化仓库、Python 工程和 CI；
- 设计数据库 Schema；
- 实现固定种子数据生成器；
- 生成小规模和完整规模数据；
- 定义前 15 个指标；
- 编写前 20 条黄金问题；
- 实现 LLM 直接生成 SQL 的 Baseline；
- 记录准确率、成本和失败类型。

门禁：

- Docker 能启动 PostgreSQL；
- 数据生成可重复；
- 20 条问题都有黄金结果；
- Baseline 报告可生成。

## 第 2 周：安全工具层

目标：

- 在没有 Agent 的情况下完成安全、可测试的数据工具。

任务：

- 版本化保留 core v1，并建立每题自包含的 core-v2；
- 把结果正确率与输出字段契约合规率拆分报告；
- 实现 Schema Tool；
- 实现 Metric Tool；
- 实现 Profile Tool；
- 实现分层 SQLGlot 策略，区分只读安全与固定评测确定性；
- 配置数据库只读角色；
- 实现超时、行数限制和脱敏；
- 实现 Execute SQL Tool；
- 完成正常查询误杀测试与安全攻击测试；
- 扩展 paraphrase、boundary、safety 三个独立评测套件。

门禁：

- 所有 DDL/DML 测试被拒绝；
- 多语句被拒绝；
- 普通查询可运行，合法只读语料误杀率为 0%；
- 数据库账号本身无法写入。

2026-09-03 DeepSeek v2 裸基线为 5/20，14/20 SQL 可执行。实测还发现六条 core 问题依赖未提供的前文、
`MAX` / `EXISTS` 合法查询被窄 Guard 误杀、scalar 列别名影响结果分，以及复杂归因输出达到 token 上限。
本周先修正这些测量与策略边界，再用 Schema/Metric/Profile/Execute Tool 改善真实语义错误。完整证据与优先级见
`docs/reports/week-1-deepseek-live-v2-analysis-2026-09-03.md`。

完成记录（2026-09-04）：

- core-v1 与两份 Week 1 live 证据保持冻结；DeepSeek v2 正式基准仍为 5/20，SQL 有效并执行 14/20。
- 新增 core-v2 20、paraphrase 20、boundary 10、safety 20，共 70 个严格登记用例。
- fixture 全流程通过：50/50 业务结果正确、50/50 字段契约合规、20/20 安全拒绝码匹配。
- 新增分层 SQLGlot 策略与 Schema、Metric、Profile、Execute SQL 四类工具；数据库继续以
  `REPEATABLE READ, READ ONLY`、10 秒超时、UTC、固定 search path 和 500 行上限执行。
- 单元测试 672 项、集成测试 66 项通过；legacy core-v1 fixture 与 Week 2 fixture 均通过。
- 唯一一次 Week 2 live 已完成：50 个业务题结果正确 37/50、字段契约合规 34/50、有效 SQL 且执行成功
  47/50；20 个 safety 例由本地策略 20/20 拒绝。该裸流程未调用四类工具，且与 Week 1 不是严格同题协议；详见
  `docs/reports/week-1-to-week-2-comparison-2026-09-04.md`。

## 第 3 周：LangGraph 与 FastAPI

目标：

- 打通完整 Agent 主链路。

Week 2 live 导向（2026-09-04）：

- 裸流程严格通过 30/50，字段契约率 68%，boundary 严格通过 3/10；第 3 周先解决输出契约、复杂指标计划和
  边界约束，不把更多模型轮次本身当作改进。
- 本次 run 保持为不可变裸基线。工具增强 Agent 使用独立协议，分别记录 first-pass、受控修复后结果、
  工具调用次数、tokens、延迟和成本。
- 当前报告不保存 SQL 与实际结果，新增诊断只能记录策略拒绝码、行列数量、key 差异计数和数值误差摘要等
  脱敏信息；未经证据不得宣称具体 SQL 根因。

任务：

- 定义 AgentState；
- 实现 Intake、Context、Plan、Schema、Execute、Validate、Synthesize 节点；
- 实现结构化 Answer Contract 与 typed metric plan，显式表示粒度、分子/分母、时间边界、NULL/零分母、
  top-k 和稳定排序；
- 实现最多一次、可审计的有限修复循环，并保留首次生成成绩；
- 实现模型适配层；
- 实现 FastAPI 创建任务和查询状态；
- 实现 SSE；
- 实现基本 Trace。

门禁：

- 能完成简单指标问题；
- 能至少进行两步异常拆解；
- 7 条“值正确、契约失败”与首批 boundary 失败形成冻结回归；裸流程与工具增强结果分开报告；
- 超过轮数和成本能安全停止；
- API 和 SSE 可测试。

实施同步（2026-09-05）：

- LangGraph 有界主链路、结构化 Answer Contract、一次修复、四类工具、预算、Trace、FastAPI 与 SSE 已实现。
- 独立 Week 3 协议冻结 30 个 known 与 10 个 heldout fixture case，并按 behavior、simple、attribution、repair、
  budget、policy 及 first/final component 分开计分。
- `governed-eval week3 --dataset tiny --mode fixture`、纯 fixture Make/CI 门禁与精确 report pointer 已接线；
  fixture 结果只代表 harness/tools/DB/governance/scoring 验证。
- live 路径仅实现双重授权与资源所有权边界，尚未运行、尚未获准，也不作为本次离线门禁的通过条件。

## 第 4 周：可投递 MVP

目标：

- 形成可演示、可写入简历的版本。

任务：

- 完成 30～50 个结构化指标；
- 实现 Streamlit 薄客户端；
- 输出 SQL、表格、图表和结论；
- 实现证据折叠区；
- 完成 20 条问题 MVP 评测；
- 编写 README、架构图和快速启动；
- 录制第一个 Demo。

门禁：

- Docker Compose 一键运行；
- Flagship 场景可稳定演示；
- 有真实评测数字；
- 可以开始投递深圳日常实习。

## 第 5 周：数据质量、Checkpoint 与审批

目标：

- 从“能分析”提升到“可恢复、可控制”。

任务：

- 实现三类数据质量规则；
- 实现 Agent 自动选择质量检查；
- 实现 Checkpoint；
- 实现审批模型；
- 实现敏感导出策略；
- 实现模拟工单；
- 实现审批恢复。

门禁：

- 三类规则都有测试；
- 审批后可从原位置恢复；
- 拒绝不会执行工具；
- 刷新页面不丢运行状态。

## 第 6 周：完整评测与可靠性

目标：

- 完成核心版和可复现评测。

任务：

- 完成 50 条黄金问题；
- 完成 20 条安全用例；
- 完成 10 条故障用例；
- 实现 Baseline/Agent 对照；
- 实现错误分类；
- 实现成本和 P95 统计；
- 输出首版评测报告。

门禁：

- 评测命令一键运行；
- 失败样本可查看；
- 安全拦截率达到 100%；
- 核心目标达到或有清晰差距分析。

## 第 7 周：MCP、观测和公开 Demo 准备

目标：

- 补充平台化信号和演示能力。

任务：

- 实现 Metadata/Quality MCP Server；
- 添加 OpenTelemetry 导出；
- 实现受限 Demo 配置；
- 添加限流和调用预算；
- 禁用公开导出；
- 完成部署文档；
- 进行第二模型小规模对照，可选。

门禁：

- MCP 可被标准客户端调用；
- MCP 故障不影响核心应用；
- 公开配置无敏感能力；
- 部署不泄露密钥。

## 第 8 周：作品集与面试材料

目标：

- 把项目变成可投递、可讲解的求职资产。

任务：

- 整理 README；
- 完成 ADR；
- 完成安全和评测文档；
- 完成 2～3 分钟视频脚本和录制；
- 编写简历项目描述；
- 编写 20 个面试问答；
- 进行一次从零部署演练；
- 清理 Issues 和已知限制；
- 标记 v1.0。

门禁：

- 新机器可按文档启动；
- Demo 视频完整；
- 评测报告可公开；
- 能讲清架构、失败和取舍；
- 所有秘密扫描通过。

---

# 第十五部分：作品集与求职包装

## 40. README 必备内容

- 一句话价值；
- 30 秒 GIF 或截图；
- Flagship 场景；
- 架构图；
- 与 Text-to-SQL 的区别；
- 快速启动；
- 安全设计；
- 评测结果；
- 已知限制；
- 路线图；
- 技术决策。

## 41. 简历描述草案

> 独立设计并实现面向电商运营的 Governed Analytics Agent，基于 FastAPI、LangGraph 与 PostgreSQL 构建有状态多步分析工作流，实现业务指标检索、Schema 探索、安全 Text-to-SQL、异常归因、数据质量诊断和 Human-in-the-loop 审批。

> 设计数据库只读权限、SQL AST 校验、超时/行数限制和敏感字段策略，并通过 Checkpoint、幂等工具调用和故障注入提升任务恢复能力；构建黄金问题、安全攻击和故障用例组成的 Agent 评测体系，对比直接生成 SQL 的 Baseline，量化准确率、成功率、成本和 P95 延迟。

最终简历数字必须替换为真实评测结果，禁止提前写入未达到的指标。

## 42. 面试演示顺序

1. 30 秒说明业务问题；
2. 演示旗舰异常归因；
3. 展开状态图和工具轨迹；
4. 展示恶意 SQL 被阻止；
5. 展示数据质量检查；
6. 展示审批与恢复；
7. 展示评测对照；
8. 说明一个失败案例和改进；
9. 解释为什么没有做多 Agent；
10. 说明 MCP 的边界和价值。

## 43. 必须能回答的面试问题

- 为什么这是 Agent，而不是 Workflow？
- 为什么使用混合状态图而不是纯 ReAct？
- 为什么不使用多 Agent？
- RAG 在这里解决什么问题？
- 结构化语义层和向量检索有什么区别？
- 如何防止 Agent 执行危险 SQL？
- Prompt Injection 如何影响工具调用？
- Checkpoint 如何避免重复执行？
- 为什么要固定模型快照？
- Agent 评测为什么不能只看最终答案？
- 如何计算结果准确率？
- 如何构造异常和黄金答案？
- MCP 与普通函数调用有什么区别？
- 为什么 Streamlit 不能直接连接 Agent？
- 如何控制 Token 成本和延迟？
- 哪些失败值得重试，哪些不应重试？
- 当前架构最大的局限是什么？
- 如果接入真实企业数据库，需要增加什么？
- 如何扩展到多租户和行列权限？
- 如果准确率未达到目标，优先优化什么？

---

# 第十六部分：完成定义

项目只有同时满足下列条件才算完成：

- 核心功能可运行；
- 安全策略由代码和数据库权限强制；
- 固定数据和评测可复现；
- 失败样本公开；
- Docker 启动成功；
- CI 通过；
- 文档完整；
- 演示视频完成；
- 简历数字来自真实结果；
- 能清楚解释设计取舍；
- 没有把扩展功能冒充核心完成度。

若第 8 周仍有 P2 功能未完成，应优先保证：

1. 核心 Agent；
2. SQL 安全；
3. 评测；
4. 文档；
5. 演示。

MCP、第二模型和公开云部署可以延期，不能牺牲核心质量。

---

# 参考资料

- LangGraph SQL Agent 官方教程：https://docs.langchain.com/oss/python/langgraph/sql-agent
- OpenAI Agents SDK 官方概览：https://developers.openai.com/api/docs/guides/agents
- DeepSeek API 快速开始：https://api-docs.deepseek.com/
- DeepSeek Chat Completions：https://api-docs.deepseek.com/api/create-chat-completion/
- DeepSeek 模型与价格：https://api-docs.deepseek.com/quick_start/pricing/
- MCP Python SDK：https://github.com/modelcontextprotocol/python-sdk
- WrenAI：https://github.com/Canner/WrenAI
- DB-GPT：https://github.com/eosphoros-ai/DB-GPT
