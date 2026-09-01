# Task 1 Report: Generator Contracts and Named Random Streams

## 实现与文件

- `pyproject.toml`：将 `numpy`、`pyyaml` 声明为直接依赖，并预注册未来的
  `governed-data` CLI entry point。
- `data/generator/tiny.yaml` 和 `full.yaml`：固定种子、UTC 业务区间和各规模计数。
- `src/governed_analytics/data_generation/models.py`：建立四个跨计划公开模型、冻结且
  校验时区/递增区间的 `GeneratorConfig`，以及稳定身份函数。
- `src/governed_analytics/data_generation/randomness.py`：通过 SHA-256 派生独立的命名
  NumPy `Generator` 流。
- `tests/unit/data_generation/`：覆盖固定配置、冻结约束、无效时间区间、identity 的稳定性
  与变化敏感性，以及全局 NumPy 状态隔离。

## TDD 证据

- RED：在模块尚不存在时运行两个新增测试文件，pytest 收集阶段均报
  `ModuleNotFoundError: No module named 'governed_analytics.data_generation'`。
- GREEN：实现后，`uv run pytest tests/unit/data_generation/test_models.py
  tests/unit/data_generation/test_randomness.py -v` 通过 8 项测试。

## Hash 规范化

`generator_config_sha256()` 对 `config.model_dump(mode="json")` 使用
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`，再对其 UTF-8
字节执行 SHA-256。`dataset_id_for_config()` 对
`"1.0.0:<config_sha256>"` 的 UTF-8 字节执行 SHA-256。测试验证同一配置稳定、配置变化会
改变两个值，且两者均为 64 位小写十六进制。

## 锁文件与质量门禁

- `uv lock` 完成；`uv.lock` 仅新增项目直接依赖 metadata 中的 `numpy` 与 `pyyaml`。
- 定向 Ruff/mypy 通过；`make check` 通过（Ruff、mypy、32 个单元测试）。
- `git diff --check` 通过。

## 自审与关注点

- 未创建或调用 CLI 实现；entry point 按裁决仅预注册，供 Task 7 落地。
- 未生成维度或事实数据，未调用网络、模型或 `.env`。
- PyYAML 本身未提供类型存根，测试导入使用精确的 `import-untyped` 忽略；未额外引入未要求的
  stub 依赖。
