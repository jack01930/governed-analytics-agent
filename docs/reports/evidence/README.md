# Live 评测证据归档

该目录把第 1 周两次 DeepSeek live 基线从被 Git 忽略的运行时 `artifacts/` 归档为可克隆、可审查的证据。

- `v1/`、`v2/` 是原始 `report.json` 与 `report.md` 的逐字节副本；SHA-256 见 `manifest.json`。
- `report.json` 包含模型生成的 SQL，但不包含 API Key、Authorization header、完整 provider 原始响应、端点凭据
  或 `.env` 内容。
- `week-1-live-baselines.json` 是删除生成 SQL 后的结构化比较快照，仍保留全部 20 个 case 的状态、分数、
  错误类别、tokens、延迟和成本。
- `week-1-live-baselines.md` 是面向人工审查的简表。

归档前对运行时目录、归档目录和分析文档执行 credential-like 模式扫描，未发现匹配。归档文件由 Git 提供
不可变历史；禁止覆盖已有 run，只允许为新 run 新增目录和 manifest 版本。
