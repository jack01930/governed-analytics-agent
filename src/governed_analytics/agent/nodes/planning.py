"""Governed context retrieval, planning, and deterministic contract compilation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel

from governed_analytics.agent.contracts import (
    ActionType,
    AnalysisType,
    AnswerContract,
    ColumnContract,
    ContextBundle,
    JsonValue,
    ObservationContract,
    ResultShape,
    SortKey,
    StopReason,
    StructuredModelRequest,
    TypedMetricPlan,
)
from governed_analytics.agent.nodes.behavior import (
    SafeDependencyError,
    cast_stop_reason,
    emit_budget_warning,
    emit_domain_event,
    failure_delta,
    finish_node,
    safe_error_stop_reason,
    sensitive_identifier,
)
from governed_analytics.agent.ports import AgentContext, StructuredInvocationError
from governed_analytics.agent.state import AgentState
from governed_analytics.runtime.budgets import BudgetExceeded
from governed_analytics.tools.contracts import MetricInfo, TableInfo

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
    metric_hypotheses = tuple(item for item in plan.hypotheses if item.kind == "metric_value")
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
            nullable=plan.zero_denominator_policy == "return_null" and plan.denominator is not None,
            unit=metric.unit,
        ),
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
        ColumnContract(name="gmv_loss", data_type="decimal", role="metric", unit=metric.unit),
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
            ColumnContract(name="delta", data_type="decimal", role="metric", unit=metric.unit),
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


def context_event_data(bundle: ContextBundle) -> Mapping[str, JsonValue]:
    return {
        "metric_count": len(bundle.metrics),
        "table_count": len(bundle.tables),
        "success": bundle.ok,
    }


def interrupted_context_event_data(
    metrics: tuple[MetricInfo, ...],
    tables: tuple[TableInfo, ...],
) -> Mapping[str, JsonValue]:
    return {
        "metric_count": len(metrics),
        "table_count": len(tables),
        "success": False,
    }


def plan_event_data(plan: TypedMetricPlan) -> Mapping[str, JsonValue]:
    return {
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "analysis_type": plan.analysis_type.value,
        "metric_id": plan.metric_id,
        "hypothesis_ids": tuple(item.hypothesis_id for item in plan.hypotheses),
    }


def _payload_models[T: BaseModel](
    payload: JsonValue | None,
    model: type[T],
) -> tuple[T, ...]:
    if not isinstance(payload, tuple):
        raise ValueError("context payload must be a tuple")
    values: list[T] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("context payload item must be an object")
        values.append(model.model_validate(dict(item)))
    return tuple(values)


async def retrieve_context(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    del state
    context = runtime.context
    started_at = context.clock.monotonic()
    observations = []
    traces = []
    metrics: tuple[MetricInfo, ...] = ()
    tables: tuple[TableInfo, ...] = ()
    context_event_attempted = False
    try:
        context.budget.consume_tool(ActionType.METRIC_LOOKUP)
        metric_invocation = await context.tools.lookup_metrics(node="retrieve_context")
        observations.append(metric_invocation.observation)
        context.trace_recorder.append_tool(metric_invocation.trace)
        traces.append(metric_invocation.trace)
        if not metric_invocation.observation.ok:
            bundle = ContextBundle(
                ok=False,
                metrics=(),
                tables=(),
                observations=tuple(observations),
            )
            context_event_attempted = True
            if not await emit_domain_event(
                context,
                node="retrieve_context",
                event_type="context.retrieved",
                data=context_event_data(bundle),
            ):
                raise SafeDependencyError("event_sink_failed")
            return finish_node(
                context=context,
                node="retrieve_context",
                started_at=started_at,
                outcome="failed",
                delta={
                    "observations": tuple(observations),
                    "tool_call_traces": tuple(traces),
                    "governance": context.budget.snapshot,
                    "stop_reason": safe_error_stop_reason(metric_invocation.observation.safe_error),
                },
            )
        metrics = _payload_models(metric_invocation.observation.payload, MetricInfo)

        context.budget.consume_tool(ActionType.SCHEMA_LOOKUP)
        schema_invocation = await context.tools.lookup_schema(node="retrieve_context")
        observations.append(schema_invocation.observation)
        context.trace_recorder.append_tool(schema_invocation.trace)
        traces.append(schema_invocation.trace)
        if schema_invocation.observation.ok:
            tables = _payload_models(schema_invocation.observation.payload, TableInfo)
        bundle = ContextBundle(
            ok=schema_invocation.observation.ok,
            metrics=metrics if schema_invocation.observation.ok else (),
            tables=tables,
            observations=tuple(observations),
        )
        context_event_attempted = True
        if not await emit_domain_event(
            context,
            node="retrieve_context",
            event_type="context.retrieved",
            data=context_event_data(bundle),
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "observations": tuple(observations),
            "tool_call_traces": tuple(traces),
            "governance": context.budget.snapshot,
        }
        if bundle.ok:
            delta["metric_context"] = metrics
            delta["schema_context"] = tables
            delta["stop_reason"] = None
        else:
            delta["stop_reason"] = safe_error_stop_reason(schema_invocation.observation.safe_error)
        return finish_node(
            context=context,
            node="retrieve_context",
            started_at=started_at,
            outcome="completed" if bundle.ok else "failed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if observations:
            delta["observations"] = tuple(observations)
        if traces:
            delta["tool_call_traces"] = tuple(traces)
        failed_lookup_bundle = (
            ContextBundle(
                ok=False,
                metrics=(),
                tables=(),
                observations=tuple(observations),
            )
            if observations and not observations[-1].ok
            else None
        )
        if failed_lookup_bundle is not None and not context_event_attempted:
            event_ok = await emit_domain_event(
                context,
                node="retrieve_context",
                event_type="context.retrieved",
                data=context_event_data(failed_lookup_bundle),
            )
        elif not context_event_attempted:
            event_ok = await emit_domain_event(
                context,
                node="retrieve_context",
                event_type="context.retrieved",
                data=interrupted_context_event_data(metrics, tables),
            )
        else:
            event_ok = True
        if not event_ok:
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        if isinstance(
            error, (BudgetExceeded, StructuredInvocationError)
        ) and not await emit_budget_warning(
            context,
            node="retrieve_context",
            reason=cast_stop_reason(delta["stop_reason"]),
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="retrieve_context",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


def _metric_prompt(metric: MetricInfo) -> Mapping[str, JsonValue]:
    return {
        "metric_id": metric.metric_id,
        "version": metric.version,
        "dimensions": metric.dimensions,
        "unit": metric.unit,
        "time_field": metric.time_field,
    }


def _table_prompt(table: TableInfo) -> Mapping[str, JsonValue]:
    return {
        "name": table.name,
        "columns": tuple(column.name for column in table.columns),
    }


_SIMPLE_HYPOTHESIS_SHAPE = (("metric_value", "metric_value", None),)
_ATTRIBUTION_HYPOTHESIS_SHAPE = (
    ("confirm_decline", "confirm_decline", None),
    ("region_contribution", "dimension_contribution", "region"),
    ("sku_contribution", "dimension_contribution", "product"),
    ("segment_contribution", "dimension_contribution", "segment"),
)


def _valid_plan_identity(state: AgentState, plan: TypedMetricPlan) -> bool:
    metric_matches = tuple(
        item
        for item in state["metric_context"]
        if item.metric_id == plan.metric_id and item.version == plan.metric_version
    )
    if len(metric_matches) != 1 or sensitive_identifier(plan.plan_id):
        return False
    hypotheses = tuple((item.hypothesis_id, item.kind, item.dimension) for item in plan.hypotheses)
    if any(sensitive_identifier(item.hypothesis_id) for item in plan.hypotheses):
        return False
    expected = (
        _SIMPLE_HYPOTHESIS_SHAPE
        if plan.analysis_type is AnalysisType.SIMPLE
        else _ATTRIBUTION_HYPOTHESIS_SHAPE
        if plan.analysis_type is AnalysisType.ATTRIBUTION
        else ()
    )
    return hypotheses == expected


async def build_plan(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    invocation = None
    try:
        request = StructuredModelRequest.for_output(
            purpose="plan",
            system_prompt=(
                "Build one bounded governed metric plan using only supplied context. "
                "Follow planning_contract exactly; hypothesis identifiers are protocol "
                "identifiers, not arbitrary labels. Copy the metric version from metrics. "
                "Use simple for scalar/grouped/Top-K values; attribution only for "
                "GMV decline diagnosis. Use UTC half-open time windows."
            ),
            user_payload={
                "query": state["normalized_query"],
                "metrics": tuple(_metric_prompt(item) for item in state["metric_context"]),
                "tables": tuple(_table_prompt(item) for item in state["schema_context"]),
                "planning_contract": {
                    "simple_hypotheses": tuple(
                        {"hypothesis_id": identity, "kind": kind, "dimension": dimension}
                        for identity, kind, dimension in _SIMPLE_HYPOTHESIS_SHAPE
                    ),
                    "attribution_hypotheses": tuple(
                        {"hypothesis_id": identity, "kind": kind, "dimension": dimension}
                        for identity, kind, dimension in _ATTRIBUTION_HYPOTHESIS_SHAPE
                    ),
                    "attribution_dimensions": ("region", "product", "segment"),
                    "simple_result_columns": "selected dimensions followed by metric_id",
                    "sorting": "sort and tie_break must reference result column identifiers",
                },
            },
            output_type=TypedMetricPlan,
            max_output_tokens=1200,
        )
        invocation = await context.model_invoker.invoke(request, TypedMetricPlan)
        plan = invocation.result.output
        if not _valid_plan_identity(state, plan):
            invalid_delta: dict[str, object] = {
                "governance": invocation.governance,
                "model_call_traces": invocation.traces,
                "stop_reason": StopReason.PLAN_INVALID,
            }
            if invocation.repair_record is not None:
                invalid_delta["repair_history"] = (invocation.repair_record,)
            return finish_node(
                context=context,
                node="build_plan",
                started_at=started_at,
                outcome="failed",
                delta=invalid_delta,
            )
        if not await emit_domain_event(
            context,
            node="build_plan",
            event_type="plan.created",
            data=plan_event_data(plan),
        ):
            raise SafeDependencyError("event_sink_failed")
        delta: dict[str, object] = {
            "plan_revisions": (plan,),
            "governance": invocation.governance,
            "model_call_traces": invocation.traces,
            "stop_reason": None,
        }
        if invocation.repair_record is not None:
            delta["repair_history"] = (invocation.repair_record,)
        if invocation.governance.soft_cap_reached:
            if not await emit_budget_warning(
                context,
                node="build_plan",
                reason=StopReason.COST_SOFT_CAP,
            ):
                raise SafeDependencyError("event_sink_failed")
            delta["stop_reason"] = StopReason.COST_SOFT_CAP
        return finish_node(
            context=context,
            node="build_plan",
            started_at=started_at,
            outcome="completed",
            delta=delta,
        )
    except Exception as error:
        delta = failure_delta(error, context)
        if invocation is not None and not isinstance(error, StructuredInvocationError):
            delta["model_call_traces"] = invocation.traces
            delta["governance"] = invocation.governance
            if invocation.repair_record is not None:
                delta["repair_history"] = (invocation.repair_record,)
        if isinstance(
            error, (BudgetExceeded, StructuredInvocationError)
        ) and not await emit_budget_warning(
            context,
            node="build_plan",
            reason=cast_stop_reason(delta["stop_reason"]),
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        return finish_node(
            context=context,
            node="build_plan",
            started_at=started_at,
            outcome="failed",
            delta=delta,
        )


async def compile_contract(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    try:
        plan = state["plan_revisions"][-1]
        matches = tuple(
            metric
            for metric in state["metric_context"]
            if metric.metric_id == plan.metric_id and metric.version == plan.metric_version
        )
        if len(matches) != 1:
            raise ValueError("plan metric binding is not unique")
        contract = compile_answer_contract(plan, matches[0])
        return finish_node(
            context=context,
            node="compile_contract",
            started_at=started_at,
            outcome="completed",
            delta={"answer_contract": contract, "stop_reason": None},
        )
    except (IndexError, KeyError, ValueError):
        return finish_node(
            context=context,
            node="compile_contract",
            started_at=started_at,
            outcome="failed",
            delta={
                "stop_reason": StopReason.PLAN_INVALID,
                "governance": context.budget.snapshot,
            },
        )
    except Exception as error:
        return finish_node(
            context=context,
            node="compile_contract",
            started_at=started_at,
            outcome="failed",
            delta=failure_delta(error, context),
        )


async def replan(
    state: AgentState,
    runtime: Runtime[AgentContext],
) -> dict[str, object]:
    context = runtime.context
    started_at = context.clock.monotonic()
    plan = state["plan_revisions"][-1]
    pending = next((item for item in plan.hypotheses if item.status == "pending"), None)
    delta: dict[str, object] = {"next_action": None}
    if pending is None:
        delta["stop_reason"] = StopReason.ANSWER_CONTRACT_UNMET
    elif context.budget.snapshot.soft_cap_reached:
        if not await emit_budget_warning(
            context,
            node="replan",
            reason=StopReason.COST_SOFT_CAP,
        ):
            delta["stop_reason"] = StopReason.INTERNAL_ERROR
        else:
            delta["stop_reason"] = StopReason.COST_SOFT_CAP
        delta["governance"] = context.budget.snapshot
    else:
        delta["stop_reason"] = None
    return finish_node(
        context=context,
        node="replan",
        started_at=started_at,
        outcome="completed",
        delta=delta,
    )


__all__ = [
    "build_plan",
    "compile_answer_contract",
    "compile_contract",
    "replan",
    "retrieve_context",
]
