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


def metric_info(metric_id: str = "gmv", *, unit: str = "cny") -> MetricInfo:
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
        unit=unit,
        version="v1",
    )


def simple_plan(
    *,
    metric_id: str = "gmv",
    dimensions: tuple[str, ...] = (),
    top_k: int | None = None,
    tie_break: tuple[str, ...] | None = None,
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
        sort=(SortKey(column=metric_id, direction="desc"),) if top_k else (),
        top_k=top_k,
        tie_break=(dimensions if tie_break is None else tie_break) if top_k else (),
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


@pytest.mark.parametrize(
    ("metric_id", "unit"),
    [
        ("net_revenue", "cny"),
        ("orders", "count"),
        ("campaign_roi", "ratio"),
    ],
)
def test_simple_metric_contract_propagates_exact_governed_unit(
    metric_id: str, unit: str
) -> None:
    contract = compile_answer_contract(
        simple_plan(metric_id=metric_id), metric_info(metric_id, unit=unit)
    ).observation_contracts[0]

    assert contract.columns[-1].unit == unit


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


@pytest.mark.parametrize("tie_break", [(), ("region",)])
def test_simple_top_k_appends_every_missing_dimension_for_total_order(
    tie_break: tuple[str, ...]
) -> None:
    contract = compile_answer_contract(
        simple_plan(
            dimensions=("region", "product"),
            top_k=5,
            tie_break=tie_break,
        ),
        metric_info(),
    ).observation_contracts[0]

    assert tuple(item.column for item in contract.order_by) == (
        "gmv",
        "region",
        "product",
    )


def test_simple_top_k_rejects_missing_key_dimensions() -> None:
    with pytest.raises(ValueError, match="key dimension"):
        compile_answer_contract(simple_plan(top_k=5), metric_info())


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
    assert tuple(column.unit for column in contract.contract("gmv_comparison").columns) == (
        "cny",
        "cny",
        "ratio",
    )
    segment = contract.contract("segment_contribution")
    assert tuple(column.unit for column in segment.columns) == (None, "cny", "cny", "cny")


@pytest.mark.parametrize(
    "replacement",
    [
        Hypothesis(hypothesis_id="confirm_decline", kind="metric_value"),
        Hypothesis(
            hypothesis_id="region_contribution",
            kind="dimension_contribution",
            dimension="product",
        ),
    ],
)
def test_attribution_rejects_wrong_hypothesis_kind_or_dimension(
    replacement: Hypothesis,
) -> None:
    plan = attribution_plan()
    hypotheses = list(plan.hypotheses)
    index = next(
        index
        for index, hypothesis in enumerate(hypotheses)
        if hypothesis.hypothesis_id == replacement.hypothesis_id
    )
    hypotheses[index] = replacement

    with pytest.raises(ValueError, match="hypotheses"):
        compile_answer_contract(
            plan.model_copy(update={"hypotheses": tuple(hypotheses)}), metric_info()
        )


@pytest.mark.parametrize("analysis_type", [AnalysisType.SIMPLE, AnalysisType.ATTRIBUTION])
def test_compiler_accepts_500_but_rejects_501_top_k(
    analysis_type: AnalysisType,
) -> None:
    if analysis_type is AnalysisType.SIMPLE:
        base = simple_plan(dimensions=("region",), top_k=500)
    else:
        base = attribution_plan().model_copy(update={"top_k": 500})

    assert compile_answer_contract(base, metric_info()).observation_contracts
    with pytest.raises(ValueError, match="500"):
        compile_answer_contract(base.model_copy(update={"top_k": 501}), metric_info())


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
