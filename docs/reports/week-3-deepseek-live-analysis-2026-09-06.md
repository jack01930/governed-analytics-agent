# Week 3 修复后 live 测试与历史对比

## 结论

完整 36 条 live 已执行并发布报告，记录通过 5/36（13.89%）。真实澄清/拒绝响应证明 JSON 请求修复已生效。
但 26 条发生 `scoring_contract_failure`，被评分回退逻辑替换为空结果，丢失运行遥测。
因此本轮不能作为完整准确率或成本基线；报告中的业务结果 0/26、工具调用 0 和预算合规 36/36 均受回退影响。

## 运行身份

- 实现提交：`2ed65e5`；运行前工作树清洁。
- Run ID：`week3-20260905T162058Z-1e86d904`
- 运行开始：UTC `2026-09-05T16:20:58.088032Z`，北京时间 `2026-09-06 00:20:58`。
- 模式：`live`；范围：`canonical`；请求模型：`deepseek-v4-flash`。
- 用例：W3K001–W3K026、W3H001–W3H010；known 26，heldout 10。
- 数据集：tiny；运行前 `make data-verify` 成功。
- Dataset ID：`a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`
- Executed manifest：`c3bcecb5714ac38dcf638969995dd9eb1e2b5bcc619682a2718aa46906785c61`
- Pricing SHA：`465b1a9e1d37e2120884045b8cc3b820d7fa8455738ff204d1fa0fb1ac9e58e2`
- JSON SHA：`8c0ef4dc4507d27b960488dc75f5a21eb32a4a1b3cd1f99bfffb956b79cae709`
- [原始 JSON](../../artifacts/evals/week3/live/20260905T162058Z-week3-20260905T162058Z-1e86d904/report.json)
- [原始 Markdown](../../artifacts/evals/week3/live/20260905T162058Z-week3-20260905T162058Z-1e86d904/report.md)

命令退出码为 0；精确指针、36 个逐例 JSON、总报告派生字段、确定性 Markdown 和安全扫描均通过核验。
这说明产物完整发布，但不表示被评分回退替换的运行事实完整。

## 可保留的结果

| 项目 | 报告记录 | 解释 |
| --- | ---: | --- |
| 总通过 | 5/36 | 26 条运行事实缺失，不是完整模型准确率 |
| known / heldout 通过 | 3/26、2/10 | 同样受评分失败影响 |
| 保留真实评分的 case | 10/36 | 其中 5 条通过、5 条不通过 |
| 评分契约失败 | 26/36 | 结果被空 `internal_error` 替换 |
| 保留终态 | 7 次 clarification_required、3 次 refused | 另 26 条为回退 internal_error |
| natural refusal/clarification | 5/10 | 覆盖非执行行为；不是纯拒绝率 |
| 已保留 input / output tokens | 4,347 / 478 | 只来自 10 个未被替换的 case |
| 已保留估算成本 | CNY 0.017248168476 | 仅部分已知成本，不能作为全轮总额 |

通过用例：W3K004、W3K007、W3K008、W3H009、W3H010。

5 条仍有真实记录的失败为：

| Case | 冻结预期 | 实际记录 |
| --- | --- | --- |
| W3K001 | 澄清 metric、time_window | 只澄清 metric |
| W3K002 | 澄清 time_window | 澄清 metric |
| W3K003 | 澄清 previous_window、current_window | 只澄清 time_window |
| W3K006 | execute | clarify，认为缺少 metric |
| W3K009 | unsupported_data_domain | clarify，认为缺少 metric |

被替换的 26 条是 W3K005、W3K010、W3K011–W3K026、W3H001–W3H008。
其中 25 条为预期 execute，1 条为预期 unsupported。不能从替换后的 0 调用/0 成本推断它们没有调用 API 或工具。
全轮实际账单需要 Provider 账单核对，原进程退出后无法由现有发布文件恢复。

## 新暴露的评分问题

代码的确定事实：`StructuredModelInvoker` 对行为/计划等结构修复使用共享 `repair_count`；
`Week3CaseResult` 则要求非 repair suite 的 `repair_count` 必须为 0，违规直接抛出校验异常。
`_rebuild_report_from_publication_evidence` 捕获评分异常后，使用没有原始 governance/trace 的 `_safe_failure`
重新评分，造成成本和调用事实清零。

离线复现：以本轮合法 W3K004 记录为基础，将 `repair_count` 置为 1，预算合规置为 false，验证器立即返回
`non-repair cases cannot claim result repair`。没有调用 API。

这证明修复计数冲突是一个确定可触发的评分缺陷，也与当前分布相符；但发布报告没有保存 26 条原始校验错误
和 trace，所以不能断言它是这 26 条的唯一共同根因。其他 scorer/schema 契约异常仍可能存在。

## 与历史对比

| 运行 | 记录 | 可以得出的结论 |
| --- | --- | --- |
| Week1 v2 live | 正式 5/20；语义审计 8/20；执行 14/20 | 意图对齐的长期参考 |
| Week2 正式 live | 结果 37/50（74%）；契约 34/50（68%）；严格 30/50（60%） | 仍是最近的完整真实业务基线 |
| Week3 上次事故 | 36 条首模型结果不可用，0/36 | 暴露 JSON 请求问题 |
| Week3 本次 live | 5 条通过、5 条已知行为错误、26 条评分失败 | 已取得真实响应，评分层成为阻断分析的新问题 |
| Week3 fixture 同范围 | 36/36；候选 strict 26/26 | 证明预编排路径；不能代表真实模型能力 |

本次与上次的可证进展是已有 10 条保留了真实响应及 usage，其中 5 条符合冻结行为契约。
不能用 13.89% 减 Week2 的 60% 宣称模型退化：评分事实缺失，问题集与执行方式也不同。
报告中 `model_unavailable=0` 仅适用于发布结果，无法为 26 条被替换的 case 排除底层 Provider 失败。

## 下一步建议

1. 先修评分记录保全：评分异常也必须保存真实安全遥测、预算与失败阶段，未知成本不能写成零。
2. 分开结构化输出修复和结果契约修复；“是否符合预期修复次数”应作为评分结果，不能拒绝记录实际发生的次数。
3. 用离线样本覆盖普通 live case 的结构修复、评分异常和失败报告发布，确保产生失败分数但不丢失事实。
4. 再分析已知的行为路由弱点：漏时间窗口、时间窗口粒度错误、误判缺少指标、unsupported 被误路由为 clarify。

本次只执行授权的一轮完整 live，没有追加挑题重跑，也没有在运行后修改评分器重算原结果。
