from __future__ import annotations

from decimal import Decimal

from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.week2_scorers import score_result_and_contract


def test_scalar_value_correctness_is_independent_of_output_alias() -> None:
    result = score_result_and_contract(
        comparison="scalar",
        key_columns=(),
        numeric_columns=("net_revenue",),
        expected=QueryResult(columns=("net_revenue",), rows=((Decimal("12.50"),),)),
        actual=QueryResult(columns=("value",), rows=(("12.50",),)),
    )

    assert result.result_score == 1
    assert not result.output_contract_conformant


def test_boolean_value_correctness_is_independent_of_output_alias() -> None:
    result = score_result_and_contract(
        comparison="boolean",
        key_columns=(),
        numeric_columns=(),
        expected=QueryResult(columns=("is_stale",), rows=((True,),)),
        actual=QueryResult(columns=("answer",), rows=((" TRUE ",),)),
    )

    assert result.result_score == 1
    assert not result.output_contract_conformant


def test_table_uses_oracle_column_positions_but_reports_alias_contract_separately() -> None:
    result = score_result_and_contract(
        comparison="table",
        key_columns=("channel",),
        numeric_columns=("conversion_rate",),
        expected=QueryResult(
            columns=("channel", "conversion_rate"),
            rows=(("web", Decimal("0.2")), ("store", Decimal("0.1"))),
        ),
        actual=QueryResult(
            columns=("source", "rate"),
            rows=(("store", "0.100001"), ("web", Decimal("0.2"))),
        ),
    )

    assert result.result_score == 1
    assert not result.output_contract_conformant


def test_top_k_requires_ordered_keys_and_numeric_values_but_not_aliases() -> None:
    result = score_result_and_contract(
        comparison="top_k",
        key_columns=("campaign_code",),
        numeric_columns=("roi",),
        expected=QueryResult(columns=("campaign_code", "roi"), rows=(("A", 3), ("B", 2), ("C", 1))),
        actual=QueryResult(
            columns=("campaign", "return"), rows=(("A", "3.000001"), ("B", 2), ("C", 1))
        ),
    )

    assert result.result_score == 1
    assert not result.output_contract_conformant


def test_top_k_rejects_reordered_or_value_mismatched_results_and_accepts_matching_empty_sets() -> (
    None
):
    expected = QueryResult(columns=("campaign_code", "roi"), rows=(("A", 3), ("B", 2)))
    reordered = QueryResult(columns=("campaign_code", "roi"), rows=(("B", 2), ("A", 3)))
    wrong_value = QueryResult(columns=("campaign_code", "roi"), rows=(("A", 4), ("B", 2)))
    assert (
        score_result_and_contract(
            comparison="top_k",
            key_columns=("campaign_code",),
            numeric_columns=("roi",),
            expected=expected,
            actual=reordered,
        ).result_score
        == 0
    )
    assert (
        score_result_and_contract(
            comparison="top_k",
            key_columns=("campaign_code",),
            numeric_columns=("roi",),
            expected=expected,
            actual=wrong_value,
        ).result_score
        == 0
    )
    empty = QueryResult(columns=("campaign_code", "roi"), rows=())
    assert (
        score_result_and_contract(
            comparison="top_k",
            key_columns=("campaign_code",),
            numeric_columns=("roi",),
            expected=empty,
            actual=empty,
        ).result_score
        == 1
    )


def test_bad_result_shape_scores_zero_even_when_aliases_match() -> None:
    result = score_result_and_contract(
        comparison="scalar",
        key_columns=(),
        numeric_columns=("gmv",),
        expected=QueryResult(columns=("gmv",), rows=((1,),)),
        actual=QueryResult(columns=("gmv",), rows=((1,), (1,))),
    )

    assert result.result_score == 0
    assert result.output_contract_conformant
