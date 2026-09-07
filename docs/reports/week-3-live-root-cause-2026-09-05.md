# Week 3 live 首次模型请求故障：归因与修复

本记录补充 `week-3-deepseek-live-incident-analysis-2026-09-05.md`，保留原事故报告的时间边界。
关联 run：`week3-20260905T094825Z-41ef87d5`。

## 直接原因及验证

生产 Behavior 请求启用了 `response_format={"type":"json_object"}`，但 system/user 消息均没有 JSON 指令。
36 个 live case 都共用这个首调用路径。DeepSeek 官方要求在消息中包含 `json` 并给出期望输出格式：
[JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)。

本次按原 W3K001 请求进行了受控诊断，仅输出安全元数据：

| 探针 | 改动 | 实测结果 |
| --- | --- | --- |
| 原 adapter | 无 | `provider_call_failed`，0/0 usage，1,488 ms |
| 原请求直接经过 SDK | 无，绕过吞异常包装以读取状态 | HTTP 400，`invalid_request_error` |
| 单变量对照 | 仅将 system 指令改为明确返回 JSON object；未加入 schema | HTTP 200，49 input / 30 output tokens，`stop` |
| 完整修复后 | 集中 JSON 指令、实际发送 schema、使用预算结算器 | 1 次调用，结构校验与结算成功，428 input / 42 output tokens，`stop`，无 repair |

单变量对照直接复现并解除了当前生产请求的 400。结合 36 条相同首调用路径，支持将缺失 JSON 指令归为
原 live 全体失败的高置信共同原因。历史 run 未保存逐条 HTTP 状态，因此不能补写成“已取得原 36 条 400 响应”。
前三次探针没有独立留存时间戳或完整响应；本表是本次工具输出的脱敏记录。

修复后探针时间为 `2026-09-05T15:34:33.897290+00:00`，Provider 返回 `deepseek-v4-flash`，
保守 usage 估算成本 CNY `0.001652912184`。前一个 HTTP 200 对照也有模型费用；两个失败探针没有 usage，
精确账单不能据此恢复。

## 同源设计缺陷

`StructuredModelRequest.for_output()` 已保存 Pydantic 的 `output_schema_summary`，但 adapter 实际只发送
system 指令和业务 payload。`prompt_bytes()` 却使用一个从未发出的 `json_schema` 信封估算预算。
因此“bound schema”只存在于本地，Provider 没有收到字段、枚举或 schema。

这一缺陷没有直接解释原 400，却会使仅修复 JSON 关键词后的输出缺乏契约指导，影响 Behavior、Plan、Action、
FinalAnswer 及 repair 请求。

## 测试为何漏过

- adapter 单测使用手写的“只返回契约 JSON。”，生产节点使用不含 JSON 的英文指令。
- Fake client 无条件返回预制合法对象，没有检查实际发送的 schema。
- fixture 直接回放结构化脚本，未经过 Provider 的 JSON Output 门禁。
- SDK 异常统一压成 `provider_call_failed`，最终报告只呈现 `model_unavailable`。

## 已实施的修复

1. 在 `StructuredModelRequest.provider_system_prompt()` 集中加入 JSON 输出指令和完整输出 schema，
   adapter 的所有节点与结构修复请求使用同一路径。
2. `prompt_bytes()` 复用相同的 system/user 内容和 `json_object` 格式，继续保留固定 512 字节协议余量。
3. 将 Behavior 的 action/reason_code/missing_fields 约束写进会随 schema 发送的描述。
4. SDK 错误仅按异常类型和状态码映射为 4xx、429、5xx、timeout、connection 或未知失败；
   不保留 SDK 原文，并在离开异常处理器后抛安全异常，阻断原 SDK 错误的 `__context__` 引用。
5. 新增真实生产指令的契约回归，覆盖四种输出类型及其 repair；补充 HTTP/传输错误分类和单次调用断言。

本次只改善 adapter 与内部 safe trace 的错误分类。Week 3 正式 JSON 尚未新增逐调用错误字段，
`model_identity_complete` 的旧派生规则也仍需后续版本化收紧；它们不应单独作为模型响应成功的证明。

## 验证结果与下一步

- 新增 8 项 JSON/schema 发送回归在修复前全部失败，修复后通过。
- `make check`：Ruff、mypy 通过，1706 个单元测试通过。
- `make test-integration`：86 个集成测试通过，包含真实数据库工具、Agent graph 与 API/SSE。
- 实际修复后探针已通过 Pydantic 结构与预算结算，证明原调用故障解除。

该探针的业务答案仍未达到 W3K001 的冻结预期：模型选择 `clarify`，只返回 `missing_fields=["metric"]`，
预期还包括 `time_window`。因此这里只确认请求与结构链路修复，不宣称 W3K001 业务通过或 Week 3 质量提升。

下一层工作是完整 live 的语义评测及安全观测字段版本化：先记录当前实现的真实行为分布，再按已知/heldout、
first/final 和错误阶段分析。应保留本次原始事故证据，使用新 Run ID 获取后续结果。
