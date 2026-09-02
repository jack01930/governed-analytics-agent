"""Strict, offline SQL fixture adapter for baseline harness validation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import sqlglot
from sqlglot import exp

from governed_analytics.evals.models import GeneratedSql
from governed_analytics.models.protocols import SqlGenerationRequest

_EXPECTED_CASE_IDS = tuple(f"G{number:03d}" for number in range(1, 21))
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _resolve_from_repository(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    object_value: dict[str, Any] = {}
    for key, value in pairs:
        if key in object_value:
            raise ValueError("invalid fixture: duplicate JSON key")
        object_value[key] = value
    return object_value


def _read_fixture(path: Path) -> Mapping[str, Any]:
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except OSError as error:
        raise ValueError("invalid fixture: unreadable") from error
    except json.JSONDecodeError as error:
        raise ValueError("invalid fixture: malformed JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("invalid fixture: top level must be an object")
    return parsed


def _parse_query(sql: str) -> exp.Query:
    if not sql.strip():
        raise ValueError("invalid fixture: invalid SQL")
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as error:
        raise ValueError("invalid fixture: invalid SQL") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("invalid fixture: invalid SQL")
    return statements[0]


def _canonical_oracle_query(case_id: str) -> exp.Query:
    path = _REPOSITORY_ROOT / "evals" / "datasets" / "golden" / "sql" / f"{case_id}.sql"
    try:
        statements = sqlglot.parse(path.read_text(encoding="utf-8"), read="postgres")
    except (OSError, sqlglot.errors.ParseError) as error:
        raise ValueError("invalid fixture: canonical Oracle unavailable") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("invalid fixture: canonical Oracle unavailable")
    return statements[0]


def _normalized_query_sql(query: exp.Query) -> str:
    """Render a parsed query after removing non-semantic SQL comments."""
    normalized = query.copy()
    for node in normalized.walk():
        node.comments = []
    return normalized.sql(dialect="postgres", pretty=False, normalize=True)


def _validate_full_fixture(sql_by_case_id: Mapping[str, Any]) -> dict[str, str]:
    if tuple(sql_by_case_id) != _EXPECTED_CASE_IDS:
        raise ValueError("invalid fixture: case IDs must be exactly G001 through G020")

    validated: dict[str, str] = {}
    for case_id in _EXPECTED_CASE_IDS:
        sql = sql_by_case_id[case_id]
        if not isinstance(sql, str):
            raise ValueError("invalid fixture: invalid SQL")
        fixture_query = _parse_query(sql)
        if _normalized_query_sql(fixture_query) != _normalized_query_sql(
            _canonical_oracle_query(case_id)
        ):
            raise ValueError("invalid fixture: Oracle equivalence failed")
        validated[case_id] = sql
    return validated


class FixtureSqlGenerator:
    """A no-network generator containing only already-validated Oracle query bodies."""

    def __init__(self, sql_by_case_id: Mapping[str, str]) -> None:
        self._sql_by_case_id = MappingProxyType(dict(sql_by_case_id))

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(self._sql_by_case_id)

    @classmethod
    def from_path(cls, path: str | Path) -> FixtureSqlGenerator:
        """Load the complete fixture mapping relative to the repository when needed."""
        raw_fixture = _read_fixture(_resolve_from_repository(path))
        return cls(_validate_full_fixture(raw_fixture))

    @classmethod
    def _from_sql_by_case_id(cls, sql_by_case_id: Mapping[str, str]) -> FixtureSqlGenerator:
        """Test-only constructor for the otherwise unreachable missing-case boundary."""
        return cls(sql_by_case_id)

    async def generate(self, request: SqlGenerationRequest) -> GeneratedSql:
        """Return preloaded SQL with deliberate zero-cost fixture metadata."""
        try:
            sql = self._sql_by_case_id[request.case_id]
        except KeyError as error:
            raise ValueError("fixture SQL unavailable") from error
        return GeneratedSql(
            sql=sql,
            assumptions=(),
            provider_model="fixture-oracle",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
        )
