"""Result and output-contract scoring for the Week 2 evaluation suites.

Unlike the frozen Week 1 scorer, value correctness and output-column naming are
reported independently.  This keeps a correct scalar or boolean value visible
when a model chose a non-canonical presentation alias.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from governed_analytics.evals.models import QueryResult

_ZERO = Decimal("0")
_ONE = Decimal("1")
type _Row = Sequence[object]
type _Key = tuple[object, ...]


class ScoreBreakdown(BaseModel):
    """Independent result and presentation-contract outcomes for one query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_score: Decimal = Field(ge=0, le=1)
    output_contract_conformant: bool


def _to_finite_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation:
            return None
    else:
        return None
    return candidate if candidate.is_finite() else None


def _numeric_equal(
    expected: object,
    actual: object,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
) -> bool:
    expected_value = _to_finite_decimal(expected)
    actual_value = _to_finite_decimal(actual)
    if expected_value is None or actual_value is None:
        return False
    difference = abs(expected_value - actual_value)
    return (
        difference <= absolute_tolerance or difference <= abs(expected_value) * relative_tolerance
    )


def _normalise_boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value == "true":
            return True
        if value == "false":
            return False
    return None


def _rows_have_width(rows: Sequence[_Row], width: int) -> bool:
    return all(len(row) == width for row in rows)


def _row_key(row: _Row, indexes: tuple[int, ...]) -> _Key | None:
    try:
        key = tuple(row[index] for index in indexes)
        if any(value is None for value in key):
            return None
        hash(key)
    except (IndexError, TypeError):
        return None
    return key


def _row_matches(
    expected: _Row,
    actual: _Row,
    numeric_indexes: tuple[int, ...],
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
) -> bool:
    if len(expected) != len(actual):
        return False
    numeric_index_set = set(numeric_indexes)
    for index, expected_value in enumerate(expected):
        if index in numeric_index_set:
            if not _numeric_equal(
                expected_value, actual[index], absolute_tolerance, relative_tolerance
            ):
                return False
        elif expected_value != actual[index]:
            return False
    return True


def _score_table(
    expected: Sequence[_Row],
    actual: Sequence[_Row],
    *,
    key_indexes: tuple[int, ...],
    numeric_indexes: tuple[int, ...],
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
) -> Decimal:
    try:
        expected_rows = tuple(expected)
        actual_rows = tuple(actual)
        if not expected_rows and not actual_rows:
            return _ONE
        width = len(expected_rows[0]) if expected_rows else len(actual_rows[0])
        if not _rows_have_width(expected_rows, width) or not _rows_have_width(actual_rows, width):
            return _ZERO
        if any(index < 0 or index >= width for index in (*key_indexes, *numeric_indexes)):
            return _ZERO
        if key_indexes:
            expected_by_key = {_row_key(row, key_indexes): row for row in expected_rows}
            actual_by_key = {_row_key(row, key_indexes): row for row in actual_rows}
            if None in expected_by_key or None in actual_by_key:
                return _ZERO
            if len(expected_by_key) != len(expected_rows) or len(actual_by_key) != len(actual_rows):
                return _ZERO
            if set(expected_by_key) != set(actual_by_key):
                return _ZERO
            pairs: Iterable[tuple[_Row, _Row]] = (
                (row, actual_by_key[key]) for key, row in expected_by_key.items()
            )
        else:
            if len(expected_rows) != len(actual_rows):
                return _ZERO
            pairs = zip(expected_rows, actual_rows, strict=True)
        return (
            _ONE
            if all(
                _row_matches(
                    expected_row,
                    actual_row,
                    numeric_indexes,
                    absolute_tolerance,
                    relative_tolerance,
                )
                for expected_row, actual_row in pairs
            )
            else _ZERO
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return _ZERO


def _score_ordered_top_k(
    expected: Sequence[_Row],
    actual: Sequence[_Row],
    *,
    key_indexes: tuple[int, ...],
    numeric_indexes: tuple[int, ...],
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
) -> Decimal:
    """Require the complete ranked answer, including its stable key order and values."""
    try:
        expected_rows = tuple(expected)
        actual_rows = tuple(actual)
        if not expected_rows and not actual_rows:
            return _ONE
        if not expected_rows or len(expected_rows) != len(actual_rows):
            return _ZERO
        width = len(expected_rows[0])
        if not _rows_have_width(expected_rows, width) or not _rows_have_width(actual_rows, width):
            return _ZERO
        if any(index < 0 or index >= width for index in (*key_indexes, *numeric_indexes)):
            return _ZERO
        for expected_row, actual_row in zip(expected_rows, actual_rows, strict=True):
            if _row_key(expected_row, key_indexes) != _row_key(actual_row, key_indexes):
                return _ZERO
            if not _row_matches(
                expected_row,
                actual_row,
                numeric_indexes,
                absolute_tolerance,
                relative_tolerance,
            ):
                return _ZERO
        return _ONE
    except (AttributeError, IndexError, TypeError, ValueError):
        return _ZERO


def score_result_and_contract(
    *,
    comparison: str,
    key_columns: tuple[str, ...],
    numeric_columns: tuple[str, ...],
    expected: QueryResult,
    actual: QueryResult,
    absolute_tolerance: Decimal = Decimal("0.01"),
    relative_tolerance: Decimal = Decimal("0.000001"),
) -> ScoreBreakdown:
    """Score values by Oracle column position and aliases as a separate contract."""
    contract = actual.columns == expected.columns
    try:
        if len(actual.columns) != len(expected.columns):
            return ScoreBreakdown(result_score=_ZERO, output_contract_conformant=contract)
        key_indexes = tuple(expected.columns.index(column) for column in key_columns)
        numeric_indexes = tuple(expected.columns.index(column) for column in numeric_columns)
        if comparison == "scalar":
            if len(expected.columns) != 1 or len(expected.rows) != 1 or len(actual.rows) != 1:
                score = _ZERO
            else:
                score = (
                    _ONE
                    if _numeric_equal(
                        expected.rows[0][0],
                        actual.rows[0][0],
                        absolute_tolerance,
                        relative_tolerance,
                    )
                    else _ZERO
                )
        elif comparison == "boolean":
            if len(expected.columns) != 1 or len(expected.rows) != 1 or len(actual.rows) != 1:
                score = _ZERO
            else:
                expected_value = _normalise_boolean(expected.rows[0][0])
                actual_value = _normalise_boolean(actual.rows[0][0])
                score = (
                    _ONE if expected_value is not None and expected_value == actual_value else _ZERO
                )
        elif comparison == "table":
            score = _score_table(
                expected.rows,
                actual.rows,
                key_indexes=key_indexes,
                numeric_indexes=numeric_indexes,
                absolute_tolerance=absolute_tolerance,
                relative_tolerance=relative_tolerance,
            )
        elif comparison == "top_k":
            score = _score_ordered_top_k(
                expected.rows,
                actual.rows,
                key_indexes=key_indexes,
                numeric_indexes=numeric_indexes,
                absolute_tolerance=absolute_tolerance,
                relative_tolerance=relative_tolerance,
            )
        else:
            score = _ZERO
    except (AttributeError, IndexError, TypeError, ValueError):
        score = _ZERO
    return ScoreBreakdown(result_score=score, output_contract_conformant=contract)


score_evaluation_result = score_result_and_contract
