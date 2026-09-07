# Live 评测证据归档

该目录把被 Git 忽略的运行时 `artifacts/` 中的 DeepSeek live 评测归档为可克隆、
可审查的证据；每个文件的字节数和 SHA-256 见 `manifest.json`。

- `v1/`、`v2/` 是第 1 周原始 `report.json` 与 `report.md` 的逐字节副本。其 `report.json`
  保留了基于合成数据生成的 SQL，但不包含 API Key、Authorization header、完整 provider
  原始响应、端点凭据或 `.env` 内容。
- `week2-live-v1/` 是第 2 周首次 live 评测的脱敏报告。它保留 case 状态、分数、安全拦截类别、
  query ID、tokens、延迟、成本及套件/实现/定价/历史参照哈希；明确不包含模型生成 SQL、
  实际查询结果、provider 原文、API 端点、凭据、Authorization 数据或 `.env` 内容。
- `week-1-live-baselines.json` 是删除生成 SQL 后的第 1 周结构化比较快照，仍保留全部 20 个
  case 的状态、分数、错误类别、tokens、延迟和成本。
- `week-1-live-baselines.md` 是面向人工审查的第 1 周简表。
- `week3-live-post-run-health-2026-09-05.json` 是 Week 3 live 事件后的脱敏只读健康检查；
  它不含凭据或端点，也不代表 live 运行发生时的 Provider 状态。

归档前对运行时目录、归档目录和分析文档执行 credential-like 模式扫描，未发现匹配。归档文件由 Git 提供
不可变历史；禁止覆盖已有 run，只允许为新 run 新增目录和 manifest 版本。

## 第三周收尾

2026-09-07 的离线门禁、fixture 报告身份、历史哈希核验及本机 HTTP/SSE 验证见
[收尾证据](week3-closeout-2026-09-07.json) 与 [交付记录](../week-3-closeout-2026-09-07.md)。
本次没有新增 live；原始报告保留在第三周 worktree 的 `artifacts/evals/` 中。
