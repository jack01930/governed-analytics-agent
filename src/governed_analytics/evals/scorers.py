"""Pure, Decimal-based result comparators for golden baseline cases."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from governed_analytics.evals.models import GoldenCase, QueryResult

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TOP_K_QUANTUM = Decimal("0.000001")
type _Row = Sequence[object]
type _Key = tuple[object, ...]


def _to_finite_decimal(value: object) -> Decimal | None:
    """Convert supported database/JSON numeric values without float arithmetic."""
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


def score_scalar(
    expected: object,
    actual: object,
    absolute_tolerance: Decimal = Decimal("0.01"),
    relative_tolerance: Decimal = Decimal("0.000001"),
) -> Decimal:
    """Score one numeric cell using exact Decimal absolute/relative tolerances."""
    return (
        _ONE if _numeric_equal(expected, actual, absolute_tolerance, relative_tolerance) else _ZERO
    )


def _row_key(row: _Row, indexes: tuple[int, ...]) -> _Key | None:
    try:
        key = tuple(row[index] for index in indexes)
        if any(value is None for value in key):
            return None
        hash(key)
    except (IndexError, TypeError):
        return None
    return key


def _rows_have_width(rows: Sequence[_Row], width: int) -> bool:
    return all(len(row) == width for row in rows)


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
        actual_value = actual[index]
        if index in numeric_index_set:
            if not _numeric_equal(
                expected_value, actual_value, absolute_tolerance, relative_tolerance
            ):
                return False
        elif expected_value != actual_value:
            return False
    return True


def score_keyed_table(
    expected: Sequence[_Row],
    actual: Sequence[_Row],
    *,
    key_indexes: tuple[int, ...],
    numeric_indexes: tuple[int, ...],
    absolute_tolerance: Decimal = Decimal("0.01"),
    relative_tolerance: Decimal = Decimal("0.000001"),
) -> Decimal:
    """Score a table, ignoring row order only when stable key columns exist."""
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


def _normalise_key(value: object) -> _Key | None:
    key = value if isinstance(value, tuple) else (value,)
    if not key or any(item is None for item in key):
        return None
    try:
        hash(key)
    except TypeError:
        return None
    return key


def score_top_k(expected: Iterable[object], actual: Iterable[object]) -> Decimal:
    """Return expected-key overlap / K, using six-decimal half-up rounding."""
    try:
        expected_keys = tuple(_normalise_key(value) for value in expected)
        actual_keys = tuple(_normalise_key(value) for value in actual)
        if (
            not expected_keys
            or any(key is None for key in expected_keys)
            or any(key is None for key in actual_keys)
        ):
            return _ZERO
        expected_set = set(expected_keys)
        if len(expected_set) != len(expected_keys):
            return _ZERO
        actual_set = set(actual_keys)
        return (Decimal(len(expected_set & actual_set)) / Decimal(len(expected_keys))).quantize(
            _TOP_K_QUANTUM, rounding=ROUND_HALF_UP
        )
    except TypeError:
        return _ZERO


def _normalise_boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _column_indexes(
    case: GoldenCase, columns: tuple[str, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    key_indexes = tuple(columns.index(column) for column in case.key_columns)
    numeric_indexes = tuple(columns.index(column) for column in case.numeric_columns)
    return key_indexes, numeric_indexes


def score_result(case: GoldenCase, expected: QueryResult, actual: QueryResult) -> Decimal:
    """Score one actual result; malformed results safely score zero, never aborting a run."""
    try:
        if actual.columns != expected.columns:
            return _ZERO
        key_indexes, numeric_indexes = _column_indexes(case, expected.columns)
        if case.comparison == "scalar":
            if len(expected.columns) != 1 or len(expected.rows) != 1 or len(actual.rows) != 1:
                return _ZERO
            return score_scalar(
                expected.rows[0][0],
                actual.rows[0][0],
                case.absolute_tolerance,
                case.relative_tolerance,
            )
        if case.comparison == "boolean":
            if len(expected.columns) != 1 or len(expected.rows) != 1 or len(actual.rows) != 1:
                return _ZERO
            expected_value = _normalise_boolean(expected.rows[0][0])
            actual_value = _normalise_boolean(actual.rows[0][0])
            return _ONE if expected_value is not None and expected_value == actual_value else _ZERO
        if case.comparison == "table":
            return score_keyed_table(
                expected.rows,
                actual.rows,
                key_indexes=key_indexes,
                numeric_indexes=numeric_indexes,
                absolute_tolerance=case.absolute_tolerance,
                relative_tolerance=case.relative_tolerance,
            )
        if case.comparison == "top_k":
            expected_keys = tuple(_row_key(row, key_indexes) for row in expected.rows)
            actual_keys = tuple(_row_key(row, key_indexes) for row in actual.rows)
            return score_top_k(expected_keys, actual_keys)
    except (AttributeError, IndexError, TypeError, ValueError):
        return _ZERO
    return _ZERO
