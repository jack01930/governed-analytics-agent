"""Load and validate the versioned Week 1 golden-question registry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlglot
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError
from sqlglot import exp

from governed_analytics.evals.models import GoldenCase

_EXPECTED_CASE_IDS = tuple(f"G{number:03d}" for number in range(1, 21))
_MULTI_ROW_CASE_IDS = frozenset(
    {"G003", "G004", "G005", "G006", "G009", "G010", "G011", "G012", "G016"}
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _resolve_from_repository(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def _read_registry(path: str | Path) -> list[dict[str, Any]]:
    resolved_path = _resolve_from_repository(path)
    try:
        parsed = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read golden registry: {resolved_path}") from error
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("golden registry must be a YAML list of mappings")
    return parsed


def _parse_case(raw_case: dict[str, Any]) -> GoldenCase:
    raw_path = raw_case.get("oracle_sql_path")
    if not isinstance(raw_path, str):
        raise ValueError("oracle_sql_path must be a string")
    case_data = {**raw_case, "oracle_sql_path": _resolve_from_repository(raw_path)}
    try:
        return GoldenCase.model_validate(case_data)
    except ValidationError as error:
        raise ValueError(str(error)) from error


def _validate_raw_case_ids(raw_cases: list[dict[str, Any]]) -> None:
    case_ids = tuple(case.get("case_id") for case in raw_cases)
    is_invalid = any(
        not isinstance(case_id, str) or re.fullmatch(r"G[0-9]{3}", case_id) is None
        for case_id in case_ids
    )
    if is_invalid:
        raise ValueError("each case ID must match G[0-9]{3}")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("golden registry contains duplicate case IDs")


def _validate_case_order(cases: tuple[GoldenCase, ...]) -> None:
    case_ids = tuple(case.case_id for case in cases)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("golden registry contains duplicate case IDs")
    if case_ids != _EXPECTED_CASE_IDS:
        raise ValueError("case IDs must be ordered G001 through G020 with none missing")
    questions = tuple(case.question for case in cases)
    if len(set(questions)) != len(questions):
        raise ValueError("golden registry contains duplicate questions")


def _outer_select(statement: exp.Query) -> exp.Select:
    if not isinstance(statement, exp.Select):
        raise ValueError("oracle SQL must use a SELECT query as its outer statement")
    return statement


def _validate_oracle_sql(case: GoldenCase) -> None:
    path = case.oracle_sql_path
    try:
        sql = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"missing oracle SQL for {case.case_id}: {path}") from error

    first_line = sql.splitlines()[0] if sql.splitlines() else ""
    required_comment = f"{case.case_id} metric_version=1.0.0"
    if not first_line.startswith("--") or required_comment not in first_line:
        raise ValueError(f"oracle SQL for {case.case_id} must start with {required_comment}")

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as error:
        raise ValueError(f"invalid PostgreSQL oracle SQL for {case.case_id}") from error
    if len(statements) != 1:
        raise ValueError(f"oracle SQL for {case.case_id} must contain exactly one statement")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise ValueError(f"oracle SQL for {case.case_id} must be a read-only Query")
    if any(isinstance(node, exp.Into) for node in statement.walk()):
        raise ValueError(f"oracle SQL for {case.case_id} must not use SELECT INTO")
    outer_select = _outer_select(statement)
    projections = tuple(outer_select.expressions)
    if not projections or any(not isinstance(projection, exp.Alias) for projection in projections):
        raise ValueError(f"oracle SQL for {case.case_id} must expose explicit output aliases")
    aliases = tuple(projection.alias for projection in projections)
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"oracle SQL for {case.case_id} has duplicate output aliases")
    declared_columns = set(case.key_columns) | set(case.numeric_columns)
    missing_columns = declared_columns - set(aliases)
    if missing_columns:
        raise ValueError(
            f"oracle SQL for {case.case_id} lacks declared output alias(es): "
            f"{', '.join(sorted(missing_columns))}"
        )
    if case.case_id in _MULTI_ROW_CASE_IDS and outer_select.args.get("order") is None:
        raise ValueError(f"multi-row oracle SQL for {case.case_id} requires deterministic ORDER BY")


def load_golden_cases(path: str | Path) -> tuple[GoldenCase, ...]:
    """Return the complete, repository-root-resolved golden-case registry."""
    raw_cases = _read_registry(path)
    _validate_raw_case_ids(raw_cases)
    cases = tuple(_parse_case(raw_case) for raw_case in raw_cases)
    _validate_case_order(cases)
    for case in cases:
        _validate_oracle_sql(case)
    return cases
