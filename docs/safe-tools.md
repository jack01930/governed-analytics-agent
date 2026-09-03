# 第 2 周安全工具层

## 目标与边界

本层在引入 Agent 循环之前提供四类小而确定的工具。它只面向固定的 12 张公开电商分析表和 15 个受治理
指标，不允许数据库写入、任意系统表访问、任意函数、原始敏感标识探索或自动 SQL 修复。

## 四类工具

| 工具 | 输入 | 输出与限制 |
| --- | --- | --- |
| Schema Tool | 可选的登记表名集合 | 按固定顺序返回表、完整字段、类型、nullable、主键、外键和枚举；不查询系统目录 |
| Metric Tool | 精确 `metric_id` | 返回版本化指标定义；未知 ID 返回 `not_found`，不做模糊猜测 |
| Profile Tool | 表、列、五类 operation、结构化过滤、可选 UTC 半开时间窗 | 值列表最多 50；过滤值按静态字段类型严格转换后参数绑定；敏感原始字段不得枚举或用于过滤 |
| Execute SQL Tool | 单条 SQL 与受限 JSON 标量参数 | 每个参数须在 SQL 中显式 `CAST` 定型；参数名、数量、字符串长度、有限数值和绝对值均有界且构造后不可原地修改；策略通过后只读执行，最多 500 行，返回规范化查询指纹而非回显 SQL |

Profile 的五类 operation 是 `time_range`、`numeric_summary`、`null_summary`、`distinct_values` 和
`top_values`。时间窗统一使用 `[start_at, end_at)`，两个边界必须都是带时区时间；数值与时间操作会检查目标
字段类型。过滤值只接受有界的 `string`、`integer`、有限浮点数或 `boolean`；不把数值字符串猜成数字，类型
不匹配会在建立数据库连接前返回 `invalid_request`。

Execute SQL 的参数类型由 SQL 自身声明，例如 `CAST(:amount AS numeric(14,2))` 或
`CAST(:start_at AS timestamptz)`。裸占位符、同名参数的冲突类型以及不支持的参数类型会在连库前拒绝；数值和
时间值随后分别转换为数据库驱动需要的精确 `Decimal` 与带时区时间。显式类型会进入规范化 SQL 和
`query_id`，参数值本身不会进入指纹。

## SQL 策略

策略在建立数据库连接前依次检查：

1. 只有一条 PostgreSQL 查询；
2. 只读 AST，不含 DDL、DML、锁、事务或配置节点；
3. 按词法作用域只访问登记的 `public` 业务表或本查询 CTE，不能用同名 CTE 掩盖系统表；
4. 只使用允许的确定性函数，并拒绝 `TABLESAMPLE` 等随机采样；
5. 不使用 `SELECT *`、本地或相关祖先作用域的整行复合值、`WITH TIES` 或非字面超限行数；
6. 拒绝系统列、`CURRENT_ROLE` / `SYSTEM_USER` 等身份关键字、对象标识类型转换、`NATURAL JOIN` 和敏感字段 `JOIN USING`；
7. 不输出或通过谓词探测 `customer_code`、`order_code`、`payment_code`、`refund_code`、`session_code`；
8. 外层行数限制收敛到 500，并生成规范化 SHA-256 `query_id`。

敏感字段只允许直接用于 `COUNT(sensitive_column)` 的粗粒度汇总；不得出现在 WHERE、HAVING、JOIN、GROUP
BY、条件表达式、distinct count 或 Profile filter 中，以免形成精确值和前缀存在性侧信道。

策略通过不等于信任 SQL。数据库仍使用 `analytics_readonly`，并在每次执行中固定：

- `REPEATABLE READ, READ ONLY`；
- `statement_timeout = 10s`；
- `search_path = public, pg_catalog`；
- `time zone = UTC`；
- 执行结束后释放独立 engine。

## 稳定错误

工具只返回 `invalid_request`、`not_found`、`sql_rejected`、`query_timeout`、`execution_failed` 和
`sensitive_result_blocked` 等稳定类别，不把 SQL、数据库异常、端点或凭据放进公开错误。成功响应必须有
`data` 且无 `error`；失败响应必须有 `error` 且无 `data`。

## 验证

```bash
uv run pytest tests/unit/tools tests/unit/safety tests/integration/tools -q
make test-integration
make eval-week2-fixture
```

当前验收覆盖全部 20 条冻结 Oracle、合法 `MAX` / `MIN` / `AVG` / `EXISTS` / `date_trunc` 样本、44 条攻击
SQL fixture、20 条活跃 safety case、敏感谓词侧信道、五类 Profile operation、公开参数绑定、拒绝前不建连、
真实只读数据库会话和 500 行上限。Week 2 fixture 的完整结论见
[Week 1 → Week 2 对比报告](reports/week-1-to-week-2-comparison-2026-09-04.md)。
