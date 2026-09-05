from __future__ import annotations

from decimal import Decimal

from governed_analytics.agent.contracts import ActionType, ToolCallTrace
from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.week3.scorers import score_candidate, score_tool_trace


def test_candidate_score_keeps_result_contract_and_strict_separate() -> None:
    score = score_candidate(
        comparison="scalar",
        key_columns=(),
        numeric_columns=("gmv",),
        expected=QueryResult(columns=("gmv",), rows=((Decimal("10.00"),),)),
        actual=QueryResult(columns=("value",), rows=((Decimal("10.00"),),)),
        answer_contract_ok=False,
        execution_succeeded=True,
        possibly_truncated=False,
    )

    assert score.result_score == 1
    assert not score.output_contract_conformant
    assert not score.strict_pass


def test_attribution_tool_order_allows_dimension_permutation() -> None:
    calls = tuple(
        ToolCallTrace(tool_name=tool, purpose=purpose, safe_arguments=arguments)
        for tool, purpose, arguments in (
            (ActionType.METRIC_LOOKUP, "metric_lookup", ()),
            (ActionType.SCHEMA_LOOKUP, "schema_lookup", ()),
            (
                ActionType.EXECUTE_SQL,
                "confirm_decline",
                (("contract_id", "gmv_comparison"), ("hypothesis_id", "confirm_decline")),
            ),
            (
                ActionType.EXECUTE_SQL,
                "segment_contribution",
                (
                    ("contract_id", "segment_contribution"),
                    ("hypothesis_id", "segment_contribution"),
                ),
            ),
            (
                ActionType.EXECUTE_SQL,
                "region_contribution",
                (("contract_id", "region_contribution"), ("hypothesis_id", "region_contribution")),
            ),
            (
                ActionType.EXECUTE_SQL,
                "sku_contribution",
                (("contract_id", "sku_contribution"), ("hypothesis_id", "sku_contribution")),
            ),
        )
    )

    score = score_tool_trace(
        required_tools=(ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP, ActionType.EXECUTE_SQL),
        forbidden_tools=(),
        tool_calls=calls,
        required_purposes=(
            "confirm_decline",
            "region_contribution",
            "sku_contribution",
            "segment_contribution",
        ),
    )

    assert score.sequence_conformant
    assert score.conformant
