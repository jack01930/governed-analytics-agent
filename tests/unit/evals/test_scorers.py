"""Contract tests for deterministic baseline-result scoring."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from governed_analytics.evals.models import GoldenCase, QueryResult
from governed_analytics.evals.scorers import (
    score_keyed_table,
    score_result,
    score_scalar,
    score_top_k,
)


def _case(
    comparison: Literal["scalar", "table", "top_k", "boolean"],
    *,
    key_columns: tuple[str, ...] = (),
    numeric_columns: tuple[str, ...] = ("value",),
    absolute_tolerance: Decimal = Decimal("0.01"),
    relative_tolerance: Decimal = Decimal("0"),
) -> GoldenCase:
    return GoldenCase(
        case_id="G001",
        question="测试问题",
        category="test",
        oracle_sql_path=Path("G001.sql"),
        comparison=comparison,
        key_columns=key_columns,
        numeric_columns=numeric_columns,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
    )


def test_scalar_uses_absolute_cent_tolerance() -> None:
    assert score_scalar(
        Decimal("100.00"), Decimal("100.009"), Decimal("0.01"), Decimal("0")
    ) == Decimal("1")
    assert score_scalar(
        Decimal("100.00"), Decimal("100.0101"), Decimal("0.01"), Decimal("0")
    ) == Decimal("0")


def test_numeric_conversion_is_decimal_finite_and_never_treats_boolean_as_numeric() -> None:
    assert score_scalar("100.00", 100, Decimal("0"), Decimal("0")) == Decimal("1")
    assert score_scalar(Decimal("1.1"), 1.1, Decimal("0"), Decimal("0")) == Decimal("1")
    for invalid in (True, "NaN", "Infinity", float("inf"), object()):
        assert score_scalar(Decimal("1"), invalid, Decimal("0.01"), Decimal("0")) == Decimal("0")


def test_scalar_uses_expected_relative_tolerance_at_boundary() -> None:
    assert score_scalar("100", "100.000100", Decimal("0"), Decimal("0.000001")) == Decimal("1")
    assert score_scalar("100", "100.000101", Decimal("0"), Decimal("0.000001")) == Decimal("0")


def test_table_is_order_independent_but_key_exact() -> None:
    expected = (("广州", Decimal("10.00")), ("深圳", Decimal("20.00")))
    actual = (("深圳", Decimal("20.00")), ("广州", Decimal("10.00")))
    assert score_keyed_table(expected, actual, key_indexes=(0,), numeric_indexes=(1,)) == Decimal(
        "1"
    )
    assert score_keyed_table(
        expected, (("北京", Decimal("10.00")),), key_indexes=(0,), numeric_indexes=(1,)
    ) == Decimal("0")


def test_table_rejects_duplicate_or_null_keys_and_compares_unkeyed_rows_in_order() -> None:
    expected = (("A", Decimal("1")), ("B", Decimal("2")))
    assert score_keyed_table(
        expected, (("A", Decimal("1")), ("A", Decimal("2"))), key_indexes=(0,), numeric_indexes=(1,)
    ) == Decimal("0")
    assert score_keyed_table(
        expected,
        ((None, Decimal("1")), ("B", Decimal("2"))),
        key_indexes=(0,),
        numeric_indexes=(1,),
    ) == Decimal("0")

    case = _case("table")
    expected_result = QueryResult(columns=("value",), rows=((Decimal("1"),), (Decimal("2"),)))
    assert score_result(
        case, expected_result, QueryResult(columns=("value",), rows=(("2",), ("1",)))
    ) == Decimal("0")


def test_top_k_returns_overlap_fraction_and_ignores_actual_duplicates() -> None:
    assert score_top_k(("A", "B", "C"), ("A", "C", "D")) == Decimal("0.666667")
    assert score_top_k(("A", "B", "C"), ("A", "A", "C")) == Decimal("0.666667")


@pytest.mark.parametrize(
    "expected,actual",
    [
        (
            QueryResult(columns=("value",), rows=((Decimal("1"),),)),
            QueryResult(columns=("other",), rows=((Decimal("1"),),)),
        ),
        (QueryResult(columns=("value",), rows=()), QueryResult(columns=("value",), rows=())),
        (
            QueryResult(columns=("value",), rows=(("bad",),)),
            QueryResult(columns=("value",), rows=(("bad",),)),
        ),
    ],
)
def test_score_result_returns_zero_for_malformed_shape_type_or_column_contract(
    expected: QueryResult, actual: QueryResult
) -> None:
    assert score_result(_case("scalar"), expected, actual) == Decimal("0")


def test_boolean_normalization_is_explicit() -> None:
    case = _case("boolean", numeric_columns=())
    expected = QueryResult(columns=("is_stale",), rows=((True,),))
    for actual_value in (True, 1, "TRUE", " true "):
        actual = QueryResult(columns=("is_stale",), rows=((actual_value,),))
        assert score_result(case, expected, actual) == Decimal("1")
    invalid_values: tuple[object, ...] = (2, "yes", Decimal("1"), None)
    for invalid_actual_value in invalid_values:
        actual = QueryResult(columns=("is_stale",), rows=((invalid_actual_value,),))
        assert score_result(case, expected, actual) == Decimal("0")


def test_score_result_dispatches_table_and_top_k_contracts() -> None:
    table_case = _case("table", key_columns=("key",), numeric_columns=("value",))
    expected_table = QueryResult(
        columns=("key", "value", "label"), rows=(("A", "1.00", "x"), ("B", "2.00", "y"))
    )
    actual_table = QueryResult(
        columns=("key", "value", "label"), rows=(("B", "2.009", "y"), ("A", 1, "x"))
    )
    assert score_result(table_case, expected_table, actual_table) == Decimal("1")

    top_k_case = _case("top_k", key_columns=("key",), numeric_columns=("value",))
    expected_top_k = QueryResult(columns=("key", "value"), rows=(("A", 9), ("B", 8), ("C", 7)))
    actual_top_k = QueryResult(
        columns=("key", "value"), rows=(("C", "not-a-number"), ("A", object()), ("A", 1))
    )
    assert score_result(top_k_case, expected_top_k, actual_top_k) == Decimal("0.666667")
