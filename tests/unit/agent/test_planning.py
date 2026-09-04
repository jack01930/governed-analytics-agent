from datetime import UTC, datetime

import pytest

from governed_analytics.agent.contracts import (
    AnalysisType,
    Hypothesis,
    ResultShape,
    SortKey,
    TimeWindow,
    TypedMetricPlan,
)
from governed_analytics.agent.nodes.planning import compile_answer_contract
from governed_analytics.tools import MetricInfo


def metric_info(metric_id: str = "gmv") -> MetricInfo:
    return MetricInfo(
        metric_id=metric_id,
        name_zh="商品交易总额",
        name_en="GMV",
        description="Paid order gross merchandise value.",
        expression_sql="sum(amount)",
        time_field="ordered_at",
        default_filters=("status = 'paid'",),
        dimensions=("region", "product", "segment"),
        source_tables=("orders",),
        unit="CNY",
        version="v1",
    )


def simple_plan(
    *,
    metric_id: str = "gmv",
    dimensions: tuple[str, ...] = (),
    top_k: int | None = None,
) -> TypedMetricPlan:
    return TypedMetricPlan(
        plan_id="simple-plan",
        revision=1,
        metric_id=metric_id,
        metric_version="v1",
        analysis_type=AnalysisType.SIMPLE,
        windows=(
            TimeWindow(
                label="current",
                start_at=datetime(2026, 6, 1, tzinfo=UTC),
                end_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ),
        dimensions=dimensions,
        null_policy="preserve",
        zero_denominator_policy="return_null",
        fill_policy="none",
        sort=(SortKey(column="gmv", direction="desc"),) if top_k else (),
        top_k=top_k,
        tie_break=dimensions if top_k else (),
        hypotheses=(Hypothesis(hypothesis_id="metric_value", kind="metric_value"),),
    )


def attribution_plan() -> TypedMetricPlan:
    return TypedMetricPlan(
        plan_id="gmv-attribution-plan",
        revision=1,
        metric_id="gmv",
        metric_version="v1",
        analysis_type=AnalysisType.ATTRIBUTION,
        windows=(
            TimeWindow(
                label="previous",
                start_at=datetime(2026, 6, 1, tzinfo=UTC),
                end_at=datetime(2026, 6, 8, tzinfo=UTC),
            ),
            TimeWindow(
                label="current",
                start_at=datetime(2026, 6, 8, tzinfo=UTC),
                end_at=datetime(2026, 6, 15, tzinfo=UTC),
            ),
        ),
        dimensions=("region", "product", "segment"),
        null_policy="preserve",
        zero_denominator_policy="return_null",
        fill_policy="none",
        sort=(SortKey(column="gmv_loss", direction="desc"),),
        top_k=10,
        tie_break=("region",),
        hypotheses=(
            Hypothesis(hypothesis_id="confirm_decline", kind="confirm_decline"),
            Hypothesis(
                hypothesis_id="region_contribution",
                kind="dimension_contribution",
                dimension="region",
            ),
            Hypothesis(
                hypothesis_id="sku_contribution",
                kind="dimension_contribution",
                dimension="product",
            ),
            Hypothesis(
                hypothesis_id="segment_contribution",
                kind="dimension_contribution",
                dimension="segment",
            ),
        ),
    )


def test_simple_plan_compiles_scalar_or_grouped_contract() -> None:
    scalar = compile_answer_contract(simple_plan(metric_id="gmv"), metric_info("gmv"))
    grouped = compile_answer_contract(
        simple_plan(metric_id="gmv", dimensions=("region",)),
        metric_info("gmv"),
    )

    assert scalar.observation_contracts[0].shape == ResultShape.SCALAR
    assert scalar.observation_contracts[0].column_names == ("gmv",)
    assert grouped.observation_contracts[0].shape == ResultShape.TABLE
    assert grouped.observation_contracts[0].column_names == ("region", "gmv")
    assert grouped.observation_contracts[0].key_columns == ("region",)


def test_simple_top_k_contract_keeps_plan_sort_and_stable_tie_break() -> None:
    contract = compile_answer_contract(
        simple_plan(dimensions=("region",), top_k=5), metric_info()
    ).observation_contracts[0]

    assert contract.shape is ResultShape.TOP_K
    assert contract.limit == contract.max_rows == 5
    assert tuple((item.column, item.direction) for item in contract.order_by) == (
        ("gmv", "desc"),
        ("region", "asc"),
    )


def test_attribution_contract_has_four_fixed_contracts() -> None:
    contract = compile_answer_contract(attribution_plan(), metric_info("gmv"))

    assert tuple(item.contract_id for item in contract.observation_contracts) == (
        "gmv_comparison",
        "region_contribution",
        "sku_contribution",
        "segment_contribution",
    )
    assert contract.contract("gmv_comparison").column_names == (
        "current_gmv",
        "previous_gmv",
        "change_rate",
    )
    assert contract.contract("region_contribution").column_names == ("region", "gmv_loss")
    assert contract.contract("sku_contribution").column_names == ("sku", "gmv_loss")
    assert contract.contract("segment_contribution").column_names == (
        "segment",
        "previous_gmv",
        "current_gmv",
        "delta",
    )
    assert contract.contract("sku_contribution").hypothesis_id == "sku_contribution"


@pytest.mark.parametrize(
    ("plan", "metric", "message"),
    [
        (simple_plan(metric_id="gmv"), metric_info("revenue"), "metric"),
        (simple_plan(dimensions=("channel",)), metric_info(), "dimension"),
        (
            attribution_plan().model_copy(update={"dimensions": ("region", "segment")}),
            metric_info(),
            "dimensions",
        ),
        (
            attribution_plan().model_copy(update={"metric_id": "revenue"}),
            metric_info("revenue"),
            "GMV",
        ),
    ],
)
def test_compiler_rejects_plan_metric_or_dimension_drift(
    plan: TypedMetricPlan, metric: MetricInfo, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        compile_answer_contract(plan, metric)
