"""Deterministic plan compilation; no query text or model output is consulted here."""

from __future__ import annotations

from typing import Literal

from governed_analytics.agent.contracts import (
    AnalysisType,
    AnswerContract,
    ColumnContract,
    ObservationContract,
    ResultShape,
    SortKey,
    TypedMetricPlan,
)
from governed_analytics.tools.contracts import MetricInfo

_ATTRIBUTION_DIMENSIONS = frozenset({"region", "product", "segment"})
_ATTRIBUTION_HYPOTHESES = (
    "confirm_decline",
    "region_contribution",
    "sku_contribution",
    "segment_contribution",
)
_DEFAULT_TOP_K = 10


def _require_metric_binding(plan: TypedMetricPlan, metric: MetricInfo) -> None:
    if plan.metric_id != metric.metric_id or plan.metric_version != metric.version:
        raise ValueError("plan metric identity or version does not match MetricInfo")


def _simple_contract(plan: TypedMetricPlan, metric: MetricInfo) -> AnswerContract:
    metric_hypotheses = tuple(
        item for item in plan.hypotheses if item.kind == "metric_value"
    )
    if len(metric_hypotheses) != 1 or len(plan.hypotheses) != 1:
        raise ValueError("simple plans require exactly one metric_value hypothesis")
    if not set(plan.dimensions).issubset(metric.dimensions):
        raise ValueError("plan dimension is not allowed by MetricInfo")
    if plan.top_k is not None and not plan.dimensions:
        raise ValueError("Top-K plans require at least one key dimension")

    column_names = (*plan.dimensions, plan.metric_id)
    available = set(column_names)
    if any(item.column not in available for item in plan.sort):
        raise ValueError("plan sort must reference result columns")
    if any(item not in available for item in plan.tie_break):
        raise ValueError("plan tie-break must reference result columns")

    order_by = list(plan.sort)
    ordered_columns = {item.column for item in order_by}
    order_by.extend(
        SortKey(column=column, direction="asc")
        for column in plan.tie_break
        if column not in ordered_columns
    )
    if plan.top_k is not None:
        ordered_columns = {item.column for item in order_by}
        order_by.extend(
            SortKey(column=column, direction="asc")
            for column in plan.dimensions
            if column not in ordered_columns
        )
        shape = ResultShape.TOP_K
        max_rows = plan.top_k
        limit = plan.top_k
    elif plan.dimensions:
        shape = ResultShape.TABLE
        max_rows = 500
        limit = None
    else:
        shape = ResultShape.SCALAR
        max_rows = 1
        limit = None

    hypothesis_id = metric_hypotheses[0].hypothesis_id
    columns = (
        *(
            ColumnContract(name=name, data_type="string", role="dimension")
            for name in plan.dimensions
        ),
        ColumnContract(
            name=plan.metric_id,
            data_type="decimal",
            role="metric",
            nullable=plan.zero_denominator_policy == "return_null"
            and plan.denominator is not None,
            unit=metric.unit,
        )
    )
    observation_contract = ObservationContract(
        contract_id=f"{hypothesis_id}_contract",
        hypothesis_id=hypothesis_id,
        columns=columns,
        shape=shape,
        min_rows=1 if shape is ResultShape.SCALAR else 0,
        max_rows=max_rows,
        key_columns=plan.dimensions,
        order_by=tuple(order_by),
        limit=limit,
    )
    return AnswerContract(
        answer_contract_id=f"{plan.plan_id}_answer",
        required_hypotheses=(hypothesis_id,),
        observation_contracts=(observation_contract,),
    )


def _top_k_contract(
    *,
    contract_id: str,
    dimension: str,
    metric_columns: tuple[ColumnContract, ...],
    order_column: str,
    order_direction: Literal["asc", "desc"],
    limit: int,
) -> ObservationContract:
    return ObservationContract(
        contract_id=contract_id,
        hypothesis_id=contract_id,
        columns=(
            ColumnContract(name=dimension, data_type="string", role="dimension"),
            *metric_columns,
        ),
        shape=ResultShape.TOP_K,
        min_rows=1,
        max_rows=limit,
        key_columns=(dimension,),
        order_by=(
            SortKey(column=order_column, direction=order_direction),
            SortKey(column=dimension, direction="asc"),
        ),
        limit=limit,
    )


def _attribution_contract(plan: TypedMetricPlan, metric: MetricInfo) -> AnswerContract:
    if metric.metric_id != "gmv":
        raise ValueError("attribution is supported only for flagship GMV")
    if set(plan.dimensions) != _ATTRIBUTION_DIMENSIONS or len(plan.dimensions) != 3:
        raise ValueError("GMV attribution dimensions must be region, product, and segment")
    if not _ATTRIBUTION_DIMENSIONS.issubset(metric.dimensions):
        raise ValueError("MetricInfo does not support all GMV attribution dimensions")
    expected_hypotheses = (
        ("confirm_decline", "confirm_decline", None),
        ("region_contribution", "dimension_contribution", "region"),
        ("sku_contribution", "dimension_contribution", "product"),
        ("segment_contribution", "dimension_contribution", "segment"),
    )
    actual_hypotheses = tuple(
        (item.hypothesis_id, item.kind, item.dimension) for item in plan.hypotheses
    )
    if actual_hypotheses != expected_hypotheses:
        raise ValueError("GMV attribution hypotheses must use the fixed governed identifiers")

    limit = plan.top_k or _DEFAULT_TOP_K
    comparison = ObservationContract(
        contract_id="gmv_comparison",
        hypothesis_id="confirm_decline",
        columns=(
            ColumnContract(
                name="current_gmv", data_type="decimal", role="metric", unit=metric.unit
            ),
            ColumnContract(
                name="previous_gmv", data_type="decimal", role="metric", unit=metric.unit
            ),
            ColumnContract(
                name="change_rate",
                data_type="decimal",
                role="metric",
                nullable=True,
                unit="ratio",
            ),
        ),
        shape=ResultShape.SINGLE_ROW,
        min_rows=1,
        max_rows=1,
    )
    monetary_metric = (
        ColumnContract(
            name="gmv_loss", data_type="decimal", role="metric", unit=metric.unit
        ),
    )
    region = _top_k_contract(
        contract_id="region_contribution",
        dimension="region",
        metric_columns=monetary_metric,
        order_column="gmv_loss",
        order_direction="desc",
        limit=limit,
    )
    sku = _top_k_contract(
        contract_id="sku_contribution",
        dimension="sku",
        metric_columns=monetary_metric,
        order_column="gmv_loss",
        order_direction="desc",
        limit=limit,
    )
    segment = _top_k_contract(
        contract_id="segment_contribution",
        dimension="segment",
        metric_columns=(
            ColumnContract(
                name="previous_gmv", data_type="decimal", role="metric", unit=metric.unit
            ),
            ColumnContract(
                name="current_gmv", data_type="decimal", role="metric", unit=metric.unit
            ),
            ColumnContract(
                name="delta", data_type="decimal", role="metric", unit=metric.unit
            ),
        ),
        order_column="delta",
        order_direction="asc",
        limit=limit,
    )
    return AnswerContract(
        answer_contract_id="gmv_attribution",
        required_hypotheses=_ATTRIBUTION_HYPOTHESES,
        observation_contracts=(comparison, region, sku, segment),
    )


def compile_answer_contract(
    plan: TypedMetricPlan,
    metric: MetricInfo,
) -> AnswerContract:
    """Compile only governed result shapes from a typed plan and metric catalog entry."""

    _require_metric_binding(plan, metric)
    if plan.top_k is not None and plan.top_k > 500:
        raise ValueError("top_k cannot exceed the 500-row QueryResult limit")
    if plan.analysis_type is AnalysisType.SIMPLE:
        return _simple_contract(plan, metric)
    if plan.analysis_type is AnalysisType.ATTRIBUTION:
        return _attribution_contract(plan, metric)
    raise ValueError("comparison plans are not supported by the Week 3 answer compiler")


__all__ = ["compile_answer_contract"]
