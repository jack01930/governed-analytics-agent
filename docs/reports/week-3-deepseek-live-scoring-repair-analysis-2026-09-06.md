# Week 3 评分保全修复后的完整 live 与历史对比

## 结论

本轮 36 条全部执行并发布，记录通过 **5/36（13.89%）**，known 3/26、heldout 2/10。
评分保全修复有效：即使 25 条再次发生评分异常，64 次模型调用、52 次工具调用、usage、成本及失败阶段均未被清零。
但本轮仍不是完整、无异常的评分基线，且 **Execute SQL 为 0 次**，不能判断真实 SQL 结果准确率。

保留下来的 trace 揭示了新的确定缺陷：工具评分器在 Metric/Schema 都完成、Execute 尚未发生时访问缺失的
Execute 位置，触发 `KeyError`。live 后已在 `2d0580e` 修复，离线重放 25 条安全工具 trace 均得到正常失败分数，
最终检查为 1720 项单元测试及 86 项集成测试通过。**本轮运行实现为 `febc12f`，不是 `2d0580e`；后续补丁未再 live。**

## 运行与证据

- Run ID：`week3-20260905T165012Z-f2e463b7`
- 开始：UTC `2026-09-05T16:50:12.789827Z`，北京时间 `2026-09-06 00:50:12`。
- 实现：`febc12f`；启动时工作树清洁。
- 协议：`week3-agent-evaluation-v1`；模式：live；范围：canonical。
- 用例：W3K001–W3K026、W3H001–W3H010，共 36 条；未挑题、未追加模型重跑。
- 模型：请求及 trace 身份均为 `deepseek-v4-flash`。
- 数据：tiny，运行前 `make data-verify` 成功；冻结 Dataset ID 为
  `a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2`。
- Executed manifest：`c3bcecb5714ac38dcf638969995dd9eb1e2b5bcc619682a2718aa46906785c61`。
- Pricing SHA：`465b1a9e1d37e2120884045b8cc3b820d7fa8455738ff204d1fa0fb1ac9e58e2`。
- JSON SHA：`fbda5aa97273cedf14c4ae59f90fb4e8a20377b4291e114654892d94371aa527`。
- [原始 JSON](../../artifacts/evals/week3/live/20260905T165012Z-week3-20260905T165012Z-f2e463b7/report.json)
- [原始 Markdown](../../artifacts/evals/week3/live/20260905T165012Z-week3-20260905T165012Z-f2e463b7/report.md)
- [核验与离线工具评分复测证据](evidence/week3-live-scoring-verification-2026-09-06.json)
- [评分修复诊断与测试说明](week-3-scoring-contract-repair-2026-09-06.md)

本次先使用标准 CLI 启动，因 worktree `.env` 未配置 Key 在模型调用前退出；随后一次配置加载也在调用前因
定价路径错误退出。实际付费运行通过相同 CLI 内部 composition `_run_week3` 发起，使用
`ModelSettings(_env_file=Path('/Users/a0000/Projects/Agent-soft/.env'))` 直接读取根目录配置，以及 CLI 的
`_PRICING_PATH` 加载冻结定价。未打印 Key、未把 Key 写入其他文件。只有上述 Run ID 发起了模型调用。

最终运行退出码为 0。核验通过：模型重新解析与派生字段一致、确定性 Markdown 一致、36 个逐例 JSON 与总报告一致、
安全扫描通过、文件权限 0600、全部逐例 LLM/tool trace 数量与账本相等、精确报告指针发布。
历史 Week3 两轮及 Week2 引用证据共 78 个文件的逐字节 SHA-256 未变。本轮报告也未因后续修复改写。

## 实际运行事实

| 项目 | 本轮记录 | 解释 |
| --- | ---: | --- |
| 通过 | 5/36 | 通过项与前一轮相同 |
| clarification_required | 7 | 5 条缺失字段/路由偏差仍在 |
| refused | 3 | 均为正确非执行行为 |
| execution_failed / plan_invalid | 24 | build_plan 阶段失败，未进入 SQL 执行 |
| internal_error | 2 | W3K016、W3H001，见下文 usage 门禁 |
| scoring_contract_failure | 25 | 工具评分缺少 Execute 存在性检查，保全回退保留真实终态 |
| 模型调用 | 64 | 62 completed、2 invalid_structure；64 次都有正数 input/output usage |
| 工具调用 | 52 | 26 次 Metric、26 次 Schema；Profile/Execute 均为 0 |
| 结构化输出修复 / 结果契约修复 | 2 / 0 | 两条 plan 结构失败各消耗一次共享修复预算 |
| natural refusal/clarification | 5/10 | 与前一轮一致；不是纯拒绝率 |
| 预算合规 | 36/36 | 按保留下来的原始账本及冻结上限计算 |

通过：W3K004、W3K007、W3K008、W3H009、W3H010。
仍有真实行为错误：W3K001 缺少 time_window 澄清；W3K002 把缺失时间窗口识别为缺失 metric；
W3K003 把两个比较窗口降为 time_window；W3K006 应执行却澄清 metric；W3K009 应 unsupported 却澄清 metric。
W3K010 应 unsupported，却进入执行路径并最终 plan_invalid。

25 条评分异常均有 Metric→Schema、无 Execute 的路径。W3K010 不要求 Execute，因此没有触发该工具评分分支；
其余 25 条预期执行用例触发缺失位置访问。该分布及修改前的离线 KeyError 复现证明这是本轮的确定可触发根因。
报告本身只记录 `scoring/scorer_exception`，未保存任意异常正文，因此归因依据是保存的 trace 与可复现代码路径。

W3K016、W3H001 的 plan 响应先出现 `invalid_structure`，随后 repair 响应完成；node trace 记录 build_plan 失败。
失败响应带有 usage，但 `fail_model_call` 只记保守成本，不增加已结算 token 计数。
`LiveWeek3CaseExecutor` 因模型 trace token 总数与账本不同，将终态替换为 `internal_error`，保留 governance/trace。
这是评分前的 runtime usage 门禁问题，与评分回退不同；本次没有修复其结算语义，也不能恢复被门禁移除的原始 behavior。

24 条 plan_invalid 表明下一阶段的业务瓶颈在计划构建/验证。现有安全报告没有计划正文及具体校验字段，
不能据此推断每条计划的精确错误，更不能把无 Execute 解释为 SQL 查询错误。

## usage 与成本口径

| 口径 | Input / Output tokens | CNY | 含义 |
| --- | ---: | ---: | --- |
| 全部模型 trace | 79,645 / 8,757 | 0.316010553936 | 使用冻结价格按全部观测 usage 重算的估算上界 |
| 已结算成功调用 | 74,975 / 8,334 | 0.298290977292 | 62 次 completed model trace |
| 2 次结构失败调用 | 4,670 / 423 | 0.079151818284 | 该金额是失败预留成本，非这部分 token 的实际价格 |
| 预算账本 committed | 74,975 / 8,334 | 0.377442795576 | 已结算成功调用成本 + 失败预留；reserved 最终为 0 |

两个 trace/账本 token 差值完整可见，没有发生评分清零。全部 usage 估算使用与运行一致的峰时 cache-miss 价格，
不考虑实际 cache 命中、时段折扣等 Provider 账单细节，因此不能称为实际扣费。
预算账本高于按已观测 usage 估算约 CNY 0.061432，体现 fail-closed 预留，不是模型额外消费的证明。

## 与 Week2 和前两轮 Week3 对比

| 运行 | 质量记录 | 模型调用 / 工具 / Execute | 可用 tokens | 成本口径 | 评价边界 |
| --- | --- | --- | ---: | --- | --- |
| Week2 正式 live | result 37/50；contract 34/50；strict 30/50 | 50 次生成；47 次执行成功 | 188,792 | 估算 CNY 0.644319 | 最近的完整业务基线，题面和协议不同 |
| Week3 第一轮 `41ef87d5` | 0/36，全部 model_unavailable | 36 / 0 / 0 | 无 Provider usage | 失败预留 CNY 0.284695 | 首模型请求链路事故 |
| Week3 第二轮 `1e86d904` | 5/36；26 条评分失败 | 只保留 10 / 0 / 0 | 4,825，部分已知 | 部分估算 CNY 0.017248 | 26 条事实丢失，不能作为总成本/完整质量基线 |
| Week3 本轮 `f2e463b7` | 5/36；25 条评分失败且遥测保全 | 64 / 52 / 0 | 88,402，全 trace | usage 估算 CNY 0.316011；预算 CNY 0.377443 | 可定位规划失败和评分边界；尚无 SQL 结果质量样本 |

本轮明确进展是可观测性和诊断：此前“26 条空 internal_error、0 次工具”被真实的上下文查询、计划失败及
模型 usage 记录替代。通过数不变，不能宣传模型质量提升；也不能用 Week3 的 13.89% 减 Week2 的 60% 宣称模型退化，
因为题面、协议、Agent 路径不同，且本轮 scorer 仍有异常。0 次 Execute 对应的有效执行率为 N/A，不是 0/0=0%。

后续优先处理 plan_invalid 的安全细分诊断，以及结构失败 usage 的 runtime 结算/身份门禁。当前
`2d0580e` 已消除本轮确认的工具评分 KeyError，但只有离线验证，未追加另一轮付费 live。
