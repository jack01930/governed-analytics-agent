# DeepSeek live 基线运行手册

## 运行边界

这是一轮裸 Text-to-SQL 基线：固定 20 题，每题恰好一次模型生成、一次 SQL 执行、一次评分；没有重试、
自动修复、工具循环、LangGraph 或人工挑选。每次报告必须原样保留。2026-09-03 的 v1、v2 报告均已完成：
v1 暴露结构化响应契约问题，v2 在修复 Prompt 和失败计量后得到 5/20、有效 SQL 14/20。

默认调用 DeepSeek 官方 OpenAI-compatible API，模型为 `deepseek-v4-flash`，显式关闭 thinking。运行会产生
外部 API 费用；历史 v2 已获授权完成，后续任何新增 live 运行仍须再次明确授权。

## 1. 本地门禁

```bash
make doctor
make check
make db-up
make migrate
make data-tiny
make data-verify
make metrics-check
make eval-fixture
make test-integration
```

必须先确认 fixture 为 20/20，数据集 manifest 未变化，集成测试全部通过。

## 2. 配置 DeepSeek

只在被 Git 忽略的 `.env` 中配置：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_BASE_URL=https://api.deepseek.com
MODEL_API_KEY=<仅写入本地的 DeepSeek API Key>
MODEL_NAME=deepseek-v4-flash
EVAL_MODEL_NAME=DeepSeek-V4-Flash-0731
```

不得把 Key 粘贴到聊天、命令历史、测试夹具、报告或 Git。`make doctor` 只检查 Key 是否存在，不显示内容。

## 3. 显式执行一次 live

```bash
uv run governed-eval baseline --dataset tiny --mode live --live
```

报告写入 `artifacts/evals/baseline/live/<UTC 时间>-<run_id>/`。不要重复执行来挑选最好成绩；只有网络或服务端
失败导致没有正式报告发布时，才在记录原因后决定是否重跑。

已完成的 v2 正式报告位于
`artifacts/evals/baseline/live/20260903T110624Z-65c87bf0355047a9b23243f70756e3f4/`，分析见
[DeepSeek 裸基线 v2 分析](reports/week-1-deepseek-live-v2-analysis-2026-09-03.md)。

## 4. 结果分析

先冻结并记录：准确率、有效 SQL 率、执行成功率、P50/P95、总/平均 tokens、估算成本和每种失败类型；然后按
题目类别统计 metric、comparison、contribution、attribution、anomaly、quality 的通过率。

| live 现象 | 主要诊断 | 第 2 周优先工作 |
| --- | --- | --- |
| `generation_invalid_content` / 空内容高 | JSON 输出或 adapter 契约不稳定 | 提示词/适配器健壮性、无重试错误边界 |
| `sql_rejected` 高 | 生成危险 SQL，或窄 Guard 误杀合法 SQL | 完整 SQLGlot 策略、攻击/误杀用例 |
| `execution_error` 高 | Schema、类型、超时或执行器问题 | Schema Tool、Profile Tool、结构化执行错误 |
| SQL 有效但 metric 题错误 | 指标口径或时间语义未被模型正确使用 | 指标检索、Schema/Metric 上下文裁剪 |
| contribution/attribution 错误集中 | 单次 SQL 不擅长多步拆解 | Profile Tool；复杂规划明确留给第 3 周 LangGraph |
| 正确率达标但 token/延迟高 | 上下文过长或输出不收敛 | 上下文预算、字段筛选和执行预算 |
| 安全拦截正常但合法 SQL 误杀 | 策略过窄 | 正常查询回归集与 AST 规则分层 |

分析结论必须把“模型能力问题”“上下文/口径问题”“SQL 策略问题”“执行器问题”分开，不能用增加重试掩盖
裸基线缺陷。第 2 周任务和新增 Golden 套件的优先级以 v2 正式报告为依据。
