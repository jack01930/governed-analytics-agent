# Text-to-SQL 基线评测

Week 1 使用 20 条版本化黄金业务问题（`G001` 到 `G020`）建立直接 Text-to-SQL 基线。每条问题有一条受控
Oracle 查询及其在 tiny 数据集上物化的只读结果；Oracle 是评测真值，不会发送给模型。模型上下文固定来自
迁移 `0001` 的 12 张公开分析表、字段/类型、主外键与必要枚举，以及固定顺序的 15 个治理指标元数据。上下文
版本和基于实际 UTF-8 上下文字节的 SHA-256 会并入 prompt version，且不含答案、Oracle SQL、异常清单、case ID、凭据或特权重置函数。

每个用例只允许一次生成、一次 SQL 执行、一次评分：没有重试、修复、工具循环或 LangGraph。SQL 先经过窄
只读 guard，再在 `analytics_readonly` 的只读、10 秒超时事务中执行。标量、表、top-k、布尔结果按列、键和
Decimal 容差评分。`passed` 表示分数 1；`wrong_answer` 表示已执行但答案不同；生成/guard 失败为
`invalid_sql`；数据库失败为 `execution_error`。输入、真值、上下文或发布失败是全局失败，不发布部分正式报告。

报告目录不可覆盖，包含 JSON、Markdown 和 20 个逐用例 JSON；汇总保留准确率、有效 SQL 率、执行成功率、
P50/P95 延迟、tokens 和精确 CNY 成本。JSON 为审计轨迹保留生成 SQL；异常原文、密钥与端点永不写入，Markdown
不嵌入 SQL。fixture 报告会明确标为
“Harness validation, not model quality.”：`make eval-fixture` 的 100% 仅验证离线评测链路，live 结果才是模型
基线。

默认安全命令为：

```bash
make data-tiny
make data-verify
make metrics-check
make eval-fixture
```

它只运行 `governed-eval baseline --dataset tiny --mode fixture`，不构造网络客户端，也不需要模型 key。CI 同样
只能运行该 fixture 命令，绝不使用 `MODEL_API_KEY`、`--mode live` 或 `--live`。

live 路径已实现但不会由本项目自动执行。它必须同时显式授权：

```bash
governed-eval baseline --dataset tiny --mode live --live
```

CLI 先拒绝 full，再检查 `--live`、非空 `MODEL_API_KEY`、设置和价格；全部通过后才会构造 OpenAI-compatible
客户端。请求名称保留配置别名 `qwen3.7-plus`，报告同时记录 provider 返回的 resolved model。默认 Base URL 是
共享北京兼容端点；拥有阿里云 Workspace ID 时，应通过 `MODEL_BASE_URL` 改为其工作空间专属端点。当前快照为
北京按量、非缓存、单次输入不超过 256K：输入 CNY 2/百万 tokens、输出 CNY 8/百万 tokens；超出该价格档立即
失败，绝不按低价静默计费。首次已授权 live 报告即使表现不佳也必须保留。

适配器使用官方 Chat Completions 接口与 OpenAI-compatible 约定：[OpenAI Chat Completions create](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions/methods/create)、[阿里云 OpenAI-compatible Chat](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[qwen3.7-plus 模型与价格](https://help.aliyun.com/zh/model-studio/qwen3-7-plus)。
