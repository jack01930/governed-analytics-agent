"""Strict registry loader for the additive Week 2 evaluation suites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import sqlglot
import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlglot import exp

from governed_analytics.evals.models import QueryResult
from governed_analytics.safety.sql_policy import SqlRejectionCode

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ACTIVE_COUNTS = {"core-v2": 20, "paraphrase": 20, "boundary": 10, "safety": 20}
_REGISTRIES = (
    "evals/datasets/golden/core-v2/cases.yaml",
    "evals/datasets/golden/paraphrase/cases.yaml",
    "evals/datasets/golden/boundary/cases.yaml",
    "evals/datasets/safety/adversarial/cases.yaml",
)
_FORBIDDEN_ORACLE_NODES = tuple(
    node
    for name in (
        "Alter",
        "Attach",
        "Cache",
        "Command",
        "Comment",
        "Commit",
        "Copy",
        "Create",
        "Delete",
        "Detach",
        "Drop",
        "Grant",
        "Insert",
        "Into",
        "LoadData",
        "Lock",
        "Merge",
        "Revoke",
        "Rollback",
        "Set",
        "Transaction",
        "TruncateTable",
        "Uncache",
        "Update",
        "Use",
    )
    if isinstance((node := getattr(exp, name, None)), type)
)
_NONDETERMINISTIC_ORACLE_NODES = (exp.CurrentDate, exp.CurrentTimestamp, exp.Rand)
_NONDETERMINISTIC_ORACLE_FUNCTION_NAMES = frozenset(
    {
        "clock_timestamp",
        "gen_random_uuid",
        "now",
        "random",
        "statement_timestamp",
        "timeofday",
        "transaction_timestamp",
    }
)


class _DuplicateKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _construct_unique_mapping(
    loader: _DuplicateKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "duplicate mapping key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class EvaluationCase(BaseModel):
    """One review-approved Week 2 case, before cross-suite reference resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[CPBS]2[0-9]{2}$")
    suite: Literal["core-v2", "paraphrase", "boundary", "safety"]
    intent_id: str = Field(pattern=r"^[CBS]2[0-9]{2}$")
    question: str | None = None
    category: str = Field(min_length=1)
    difficulty: Literal["easy", "medium", "hard"]
    risk_tags: tuple[str, ...] = ()
    expected_behavior: Literal["execute", "reject"]
    source: str = Field(min_length=1)
    review_status: Literal["approved"]
    legacy_case_id: str | None = Field(default=None, pattern=r"^G[0-9]{3}$")
    comparison: Literal["scalar", "table", "top_k", "boolean"] | None = None
    key_columns: tuple[str, ...] = ()
    numeric_columns: tuple[str, ...] = ()
    oracle_sql_path: Path | None = None
    oracle_ref: str | None = Field(default=None, pattern=r"^C2[0-9]{2}$")
    expected_result_path: Path | None = None
    candidate_sql: str | None = None
    expected_rejection: SqlRejectionCode | None = None

    @model_validator(mode="after")
    def _validate_case_contract(self) -> EvaluationCase:
        prefix = {"core-v2": "C", "paraphrase": "P", "boundary": "B", "safety": "S"}[self.suite]
        if not self.case_id.startswith(prefix):
            raise ValueError("case_id prefix must match suite")
        if not self.category.strip() or not self.source.strip() or not self.risk_tags:
            raise ValueError("category, source, and risk_tags must not be blank")
        if any(not tag.strip() for tag in self.risk_tags):
            raise ValueError("risk_tags must not contain blank values")
        if len(set(self.key_columns)) != len(self.key_columns) or len(
            set(self.numeric_columns)
        ) != len(self.numeric_columns):
            raise ValueError("output columns must not contain duplicates")
        if set(self.key_columns) & set(self.numeric_columns):
            raise ValueError("key_columns and numeric_columns must not overlap")
        if self.expected_behavior == "execute":
            if self.question is None or not self.question.strip():
                raise ValueError("execute cases require a nonblank question")
            if self.comparison is None or (self.oracle_sql_path is None) == (
                self.oracle_ref is None
            ):
                raise ValueError("execute cases require exactly one Oracle source")
            if self.candidate_sql is not None or self.expected_rejection is not None:
                raise ValueError("execute cases cannot declare safety SQL")
            if self.comparison == "top_k" and not self.key_columns:
                raise ValueError("top_k cases require key_columns")
            if self.comparison in {"scalar", "boolean"} and self.key_columns:
                raise ValueError("scalar and boolean cases cannot declare key_columns")
            if self.comparison == "boolean" and self.numeric_columns:
                raise ValueError("boolean cases cannot declare numeric_columns")
        else:
            if any(
                value is not None
                for value in (
                    self.question,
                    self.comparison,
                    self.oracle_sql_path,
                    self.oracle_ref,
                    self.expected_result_path,
                )
            ):
                raise ValueError("safety cases cannot declare an execute contract")
            if self.key_columns or self.numeric_columns or self.legacy_case_id is not None:
                raise ValueError("safety cases cannot declare result metadata")
            if self.candidate_sql is None or not self.candidate_sql.strip():
                raise ValueError("safety cases require candidate_sql")
        if self.suite == "core-v2":
            if (
                self.intent_id != self.case_id
                or self.legacy_case_id is None
                or self.oracle_ref is not None
            ):
                raise ValueError(
                    "core-v2 cases require matching intent, legacy mapping, and direct Oracle"
                )
        elif self.suite == "paraphrase":
            if self.oracle_ref != self.intent_id or self.legacy_case_id is not None:
                raise ValueError("paraphrase cases must reference their core-v2 intent Oracle")
        elif self.suite == "boundary" and self.intent_id != self.case_id:
            raise ValueError("boundary cases must use their own intent_id")
        elif self.suite == "safety" and self.intent_id != self.case_id:
            raise ValueError("safety cases must use their own intent_id")
        if self.suite == "boundary" and self.expected_result_path is None:
            raise ValueError("boundary cases require expected_result_path")
        return self


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def _read_registry(path: str | Path) -> list[dict[str, Any]]:
    try:
        raw = yaml.load(_resolve(path).read_text(encoding="utf-8"), Loader=_DuplicateKeySafeLoader)
    except (OSError, TypeError, yaml.YAMLError) as error:
        raise ValueError("invalid Week 2 evaluation registry YAML") from error
    if not isinstance(raw, list) or not all(isinstance(case, dict) for case in raw):
        raise ValueError("Week 2 evaluation registry must be a YAML list of mappings")
    return raw


def _parse_case(raw: dict[str, Any]) -> EvaluationCase:
    path = raw.get("oracle_sql_path")
    if path is not None:
        if not isinstance(path, str):
            raise ValueError("oracle_sql_path must be a string")
        raw = {**raw, "oracle_sql_path": _resolve(path)}
    expected_path = raw.get("expected_result_path")
    if expected_path is not None:
        if not isinstance(expected_path, str):
            raise ValueError("expected_result_path must be a string")
        raw = {**raw, "expected_result_path": _resolve(expected_path)}
    try:
        return EvaluationCase.model_validate(raw)
    except ValidationError as error:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors(include_context=False, include_input=False, include_url=False)
        )
        raise ValueError(f"invalid Week 2 evaluation case: {detail}") from error


def _validate_oracle_path(case: EvaluationCase) -> None:
    if case.oracle_sql_path is None:
        return
    try:
        resolved = case.oracle_sql_path.resolve(strict=True)
        resolved.relative_to(_REPOSITORY_ROOT.resolve())
    except (OSError, ValueError):
        raise ValueError(
            f"oracle SQL path is unavailable or outside repository for {case.case_id}"
        ) from None
    if not resolved.is_file() or resolved.suffix != ".sql":
        raise ValueError(f"oracle SQL path is unavailable or outside repository for {case.case_id}")
    _validate_oracle_sql(case, resolved)


def _validate_oracle_sql(case: EvaluationCase, path: Path) -> None:
    try:
        sql = path.read_text(encoding="utf-8")
        statements = sqlglot.parse(sql, read="postgres")
    except (OSError, UnicodeDecodeError, sqlglot.errors.SqlglotError):
        raise ValueError(f"invalid Oracle SQL for {case.case_id}") from None
    expected_id = case.legacy_case_id or case.case_id
    first_line = sql.splitlines()[0] if sql.splitlines() else ""
    if not first_line.startswith("--") or expected_id not in first_line:
        raise ValueError(f"Oracle SQL comment must identify {expected_id}")
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError(f"Oracle SQL must be exactly one read-only query for {case.case_id}")
    statement = statements[0]
    if any(isinstance(node, _FORBIDDEN_ORACLE_NODES) for node in statement.walk()):
        raise ValueError(f"Oracle SQL contains a forbidden node for {case.case_id}")
    if any(
        isinstance(node, _NONDETERMINISTIC_ORACLE_NODES)
        or (
            isinstance(node, exp.Func)
            and node.sql_name().lower() in _NONDETERMINISTIC_ORACLE_FUNCTION_NAMES
        )
        for node in statement.walk()
    ):
        raise ValueError(f"Oracle SQL is nondeterministic for {case.case_id}")
    if not isinstance(statement, exp.Select):
        raise ValueError(f"Oracle SQL must have a SELECT outer query for {case.case_id}")
    projections = tuple(statement.expressions)
    if not projections or any(not isinstance(projection, exp.Alias) for projection in projections):
        raise ValueError(f"Oracle SQL requires explicit output aliases for {case.case_id}")
    aliases = tuple(projection.alias for projection in projections)
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"Oracle SQL has duplicate output aliases for {case.case_id}")
    declared_columns = set(case.key_columns) | set(case.numeric_columns)
    if not declared_columns.issubset(aliases):
        raise ValueError(f"Oracle SQL omits declared output aliases for {case.case_id}")
    if case.key_columns:
        order = statement.args.get("order")
        ordered_columns = (
            set() if order is None else {column.name for column in order.find_all(exp.Column)}
        )
        if not set(case.key_columns).issubset(ordered_columns):
            raise ValueError(f"multi-row Oracle ORDER BY must cover key columns for {case.case_id}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate expected JSON key")
        result[key] = value
    return result


def _validate_expected_result_path(case: EvaluationCase) -> None:
    if case.expected_result_path is None:
        return
    try:
        path = case.expected_result_path.resolve(strict=True)
        path.relative_to(_REPOSITORY_ROOT.resolve())
        raw = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
        result = QueryResult.model_validate_json(json.dumps(raw), strict=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ValidationError):
        raise ValueError(f"expected result is unavailable or invalid for {case.case_id}") from None
    declared_columns = set(case.key_columns) | set(case.numeric_columns)
    if not declared_columns.issubset(result.columns):
        raise ValueError(f"expected result omits declared columns for {case.case_id}")
    if case.comparison in {"scalar", "boolean"} and (
        len(result.columns) != 1 or len(result.rows) != 1
    ):
        raise ValueError(f"expected result has invalid scalar shape for {case.case_id}")
    if case.comparison == "boolean" and not isinstance(result.rows[0][0], bool):
        raise ValueError(f"expected result has invalid boolean shape for {case.case_id}")


def load_week2_cases(paths: tuple[str | Path, ...] = _REGISTRIES) -> tuple[EvaluationCase, ...]:
    """Load all active suites, validate cross-suite references, and preserve ordering."""
    cases = tuple(_parse_case(raw) for path in paths for raw in _read_registry(path))
    case_ids = tuple(case.case_id for case in cases)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("Week 2 evaluation registries contain duplicate case IDs")
    questions = tuple(case.question for case in cases if case.question is not None)
    if len(set(questions)) != len(questions):
        raise ValueError("Week 2 evaluation registries contain duplicate questions")
    counts = {suite: sum(case.suite == suite for case in cases) for suite in _ACTIVE_COUNTS}
    if paths == _REGISTRIES and counts != _ACTIVE_COUNTS:
        raise ValueError("Week 2 active registry has an unexpected suite count")
    cases_by_id = {case.case_id: case for case in cases}
    for case in cases:
        _validate_oracle_path(case)
        _validate_expected_result_path(case)
        if case.oracle_ref is not None:
            source = cases_by_id.get(case.oracle_ref)
            if source is None or source.suite != "core-v2" or source.oracle_sql_path is None:
                raise ValueError(
                    f"oracle_ref must resolve to a core-v2 direct Oracle for {case.case_id}"
                )
    legacy_ids = tuple(case.legacy_case_id for case in cases if case.suite == "core-v2")
    expected_legacy_ids = tuple(f"G{number:03d}" for number in range(1, 21))
    if paths == _REGISTRIES and legacy_ids != expected_legacy_ids:
        raise ValueError("core-v2 must map one-to-one and in order to G001 through G020")
    return cases


def load_active_suites() -> tuple[EvaluationCase, ...]:
    """Load the fixed 70-case Week 2 active registry."""
    return load_week2_cases()


def resolved_oracle_path(case: EvaluationCase, cases: tuple[EvaluationCase, ...]) -> Path:
    """Return an execute case's direct or referenced Oracle path after registry validation."""
    if case.oracle_sql_path is not None:
        return case.oracle_sql_path
    by_id = {item.case_id: item for item in cases}
    if case.oracle_ref is not None and case.oracle_ref in by_id:
        source = by_id[case.oracle_ref]
        if source.oracle_sql_path is not None:
            return source.oracle_sql_path
    raise ValueError(f"case has no resolved Oracle path: {case.case_id}")
