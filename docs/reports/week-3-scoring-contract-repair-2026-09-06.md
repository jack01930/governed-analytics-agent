# Week 3 评分契约与遥测保全修复

## 诊断结论

前一轮 `week3-20260905T162058Z-1e86d904` 的 26 条 `scoring_contract_failure` 无法从已发布文件恢复原始运行事实。
本次不重算、不修改该 run，也不声称已证明 26 条的全部具体异常相同。已确认的实现缺陷如下：

1. `StructuredModelInvoker` 的 JSON/schema 修复与 execution 节点的结果契约修复都调用
   `BudgetLedger.consume_repair()`，共用没有分类的 `repair_count`。
2. 评分器把这个总数与冻结用例的预期结果修复次数比较；`Week3CaseResult` 又要求普通 suite 总数为 0、
   repair suite 恰好为 1。因此真实发生的修复会触发记录模型的校验错误，正常失败结果也无法记录。
3. `_rebuild_report_from_publication_evidence` 捕获任意评分异常后，重新给空 `_safe_failure` 评分。
   这一步覆盖了原始 behavior、终态、trace、调用数、tokens 和成本，且可能在第二次评分时再次抛错。
4. 同类风险还包括 Execute 缺少对应 validation、trace 与账本调用数不一致、评分函数本身异常。
   这些事实应被保留为失败证据，不能要求先满足成功结果的关联契约才允许发布。
5. `_score_case` 原来的模型身份判断遗漏了 trace 调用数与账本 LLM 调用数相等这一条件，与报告模型的
   校验逻辑不一致；现已统一。

## 实现后的契约

- `repair_count` 保持共享预算总数及现有 API 含义；新增 `structured_output_repair_count`，只在结构化输出
  修复入口记账。结果契约修复次数为两者之差，在报告中公开为 `result_contract_repair_count`。
- 两种修复仍共享原有 `max_repairs=1`；不会因为分类而获得额外重试。冻结协议、Prompt、模型、用例和
  Oracle 均不改变。
- 预算评分同时检查总修复次数上限和结果契约修复次数是否符合预期。超限、非预期修复、预期修复未发生
  都可以正常记录，只产生不合规分数。结构化计数不能超过总数这一事实约束仍保留。
- 评分异常由独立 `_record_scoring_failure` 记录，不再次调用 scorer；保留原始安全 behavior/终态、
  model/tool/node trace、usage、模型身份、共享修复总数及分类、预算上限、已记账成本和未结算预留。
- `scoring_failure` 记录 `scoring` 或 `report_validation` 阶段、固定异常类别、白名单 validation code、
  校验诊断摘要 SHA-256 和原始运行错误码。不发布异常正文、Pydantic 输入、Prompt、SQL、原始结果或凭据。
- 失败记录的候选/证据评分采用保守失败占位，维持适用分母；`scoring_available=false`，禁止通过、
  结果正确、已验证证据或修复成功等声明。关联校验失败时保留安全工具 trace，不能把未验证关联伪装成有效证据。
  安全 trace 和账本保留各自事实，即使二者不一致也不强行改成相同。
- `committed_cost_cny` 沿用预算账本语义：可能含失败调用的保守预留，不等于 Provider 实际账单。
  `reserved_cost_cny` 单独保留，预算硬上限检查包含二者。model trace 的 outcome、usage 与估算成本用于识别
  已知调用与失败调用；未知 usage/账单不能解释为免费调用。
- 本次新增报告字段属于向后读取可缺省的扩展，不把旧 run 的空字段补写成已知事实。

## 离线验证

- `make check`：Ruff 通过、mypy 158 个文件通过、1716 项单元测试通过。
- `make test-integration`：86 项通过，包括完整 Week3 fixture、API/SSE、数据库受控执行与发布链路。
- 新增回归覆盖：结构修复分类；两类修复共享预算；普通用例的结构修复仍可通过；非预期结果修复和超限次数
  可发布为失败；Execute 缺失 validation 保留实际调用；candidate/behavior/suite scorer 异常保留非零 usage、
  成本及预留、原始完成终态、trace、候选/证据分母；逐例 JSON 与总报告一致；安全哨兵不泄漏；
  全部 40 个 fixture case 在 scorer 完全失效时仍能发布 canonical 失败报告。
- 运行新 live 前，对历史 Week3 两轮和 Week2 引用证据的 78 个文件记录逐字节 SHA-256，运行后再次比对。

新的完整 live 及历史对比另行记录，避免混入历史报告的原始结论。
