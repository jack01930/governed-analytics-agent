from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, cast

import pytest
from langgraph.errors import NodeCancelledError
from langgraph.runtime import Runtime

import governed_analytics.agent.graph as agent_graph_module
from governed_analytics.agent import (
    ActionType,
    AgentAction,
    AgentContext,
    AgentState,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    ModelPurpose,
    ModelUsage,
    Observation,
    SafeTrace,
    StopReason,
    StructuredModelRequest,
    StructuredModelResult,
    ToolCallTrace,
    ToolInvocation,
    TypedMetricPlan,
    build_agent_graph,
    new_agent_state,
    run_agent,
)
from governed_analytics.agent.contracts import AgentModelErrorCategory
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.nodes.behavior import map_agent_failure
from governed_analytics.agent.nodes.execution import route_action
from governed_analytics.agent.nodes.planning import compile_answer_contract
from governed_analytics.agent.nodes.synthesis import finalize
from governed_analytics.agent.nodes.synthesis import synthesize as synthesize_node
from governed_analytics.agent.ports import AgentModelError
from governed_analytics.agent.tool_registry import ToolRegistry
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.models.agent_fixtures import AgentScripts, ScriptedAgentModel
from governed_analytics.pricing import ModelPricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits
from governed_analytics.safety.sql_policy import (
    SqlPolicyError,
    SqlRejectionCode,
    ValidatedSql,
)
from governed_analytics.tools import (
    ErrorCode,
    ExecuteSqlTool,
    MetricInfo,
    MetricTool,
    ProfileRequest,
    ProfileResult,
    ProfileTool,
    QueryResult,
    SchemaTool,
    ToolError,
    ToolResponse,
)

QUERY = "2026年6月GMV是多少？"  # noqa: RUF001
ATTRIBUTION_QUERY = "为什么2026年6月第二周GMV比第一周下降？"  # noqa: RUF001
CLARIFY_QUERY = "GMV怎么样？"  # noqa: RUF001
QUERY_ID = "a" * 64
SynthesisFactory = Callable[[StructuredModelRequest], Mapping[str, object]]
RAW_INVALID_TOOL_RESULT_SENTINEL = "raw_invalid_tool_result_sentinel"
CLASS_GETTER_SENTINEL = "raw_class_getter_sentinel"
MALFORMED_FIELD_SENTINEL = "raw_malformed_field_sentinel"
MAPPING_READ_SENTINEL = "raw_mapping_read_sentinel"


class InvalidToolReturn:
    def __repr__(self) -> str:
        return RAW_INVALID_TOOL_RESULT_SENTINEL


class ExplodingClassProxy:
    observation_reads = 0
    trace_reads = 0

    @property  # type: ignore[misc]
    def __class__(self) -> type[object]:  # type: ignore[override]
        raise RuntimeError(CLASS_GETTER_SENTINEL)

    @property
    def observation(self) -> object:
        type(self).observation_reads += 1
        raise RuntimeError(CLASS_GETTER_SENTINEL)

    @property
    def trace(self) -> object:
        type(self).trace_reads += 1
        raise RuntimeError(CLASS_GETTER_SENTINEL)


class CountingToolInvocation(ToolInvocation):
    observation_reads: ClassVar[int] = 0
    trace_reads: ClassVar[int] = 0

    def __getattribute__(self, name: str) -> object:
        if name == "observation":
            type(self).observation_reads += 1
        elif name == "trace":
            type(self).trace_reads += 1
        return super().__getattribute__(name)


class CountingObservation(Observation):
    field_reads: ClassVar[int] = 0

    def __getattribute__(self, name: str) -> object:
        if name in type(self).model_fields:
            type(self).field_reads += 1
        return super().__getattribute__(name)


class CountingToolCallTrace(ToolCallTrace):
    field_reads: ClassVar[int] = 0

    def __getattribute__(self, name: str) -> object:
        if name in type(self).model_fields:
            type(self).field_reads += 1
        return super().__getattribute__(name)


class MalformedField:
    def __repr__(self) -> str:
        return MALFORMED_FIELD_SENTINEL


class ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        del key
        raise RuntimeError(MAPPING_READ_SENTINEL)

    def __iter__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError(MAPPING_READ_SENTINEL)

    def __len__(self) -> int:
        raise RuntimeError(MAPPING_READ_SENTINEL)


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 4, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0


class RecordingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, JsonValue]]] = []

    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        self.items.append((node, event_type, dict(data)))


class FailingEvents(RecordingEvents):
    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        del node, event_type, data
        raise RuntimeError("raw event sink sentinel")


class FailOneEvent(RecordingEvents):
    def __init__(self, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type

    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        if event_type == self.event_type:
            raise RuntimeError("raw selected event sentinel")
        await super().emit(node, event_type, data)


class FailNthEvent(RecordingEvents):
    def __init__(self, event_type: str, occurrence: int) -> None:
        super().__init__()
        self.event_type = event_type
        self.occurrence = occurrence
        self.seen = 0

    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        if event_type == self.event_type:
            self.seen += 1
            if self.seen == self.occurrence:
                raise RuntimeError("raw nth event sentinel")
        await super().emit(node, event_type, data)


class FailFirstEvent(RecordingEvents):
    def __init__(self, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self.attempts = 0

    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        if event_type == self.event_type:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("raw first event sentinel")
        await super().emit(node, event_type, data)


class FailingTraceRecorder(InMemoryTraceRecorder):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    def append_node(self, trace):  # type: ignore[no-untyped-def]
        if (
            self.failure == "append_node"
            or (self.failure == "append_node_invoke_tool" and trace.node == "invoke_tool")
            or (
                self.failure == "append_node_validate_observation"
                and trace.node == "validate_observation"
            )
            or (self.failure == "append_node_judge_evidence" and trace.node == "judge_evidence")
            or (self.failure == "append_node_synthesize" and trace.node == "synthesize")
        ):
            raise RuntimeError("raw append node sentinel")
        return super().append_node(trace)

    def append_model(self, traces):  # type: ignore[no-untyped-def]
        if self.failure == "append_model":
            raise RuntimeError("raw append model sentinel")
        return super().append_model(traces)

    def append_tool(self, trace):  # type: ignore[no-untyped-def]
        if self.failure == "append_tool" and trace.tool_name is ActionType.EXECUTE_SQL:
            raise RuntimeError("raw append tool sentinel")
        return super().append_tool(trace)

    def snapshot(self) -> SafeTrace:
        if self.failure == "snapshot":
            raise RuntimeError("raw snapshot sentinel")
        return super().snapshot()


class AppendThenFailTraceRecorder(InMemoryTraceRecorder):
    def append_node(self, trace):  # type: ignore[no-untyped-def]
        super().append_node(trace)
        if trace.node == "intake":
            raise RuntimeError("raw append after commit sentinel")


class SequenceBackend:
    def __init__(self, results: Sequence[QueryResult]) -> None:
        self.results = list(results)
        self.calls = 0

    async def execute(
        self,
        _validated: ValidatedSql,
        _parameters: tuple[object, ...],
    ) -> QueryResult:
        self.calls += 1
        return self.results.pop(0)


class RecordingProfileTool(ProfileTool):
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: ProfileRequest) -> ToolResponse[ProfileResult]:
        self.calls += 1
        return ToolResponse(
            ok=True,
            data=ProfileResult(
                query_id="b" * 64,
                table_name=request.table_name,
                column_name=request.column_name,
                operation=request.operation,
                columns=("value", "value_count"),
                rows=(("north", 3),),
                row_count=1,
                value_limit=request.limit,
            ),
        )


class RecordingTools:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.calls: list[ActionType] = []

    async def lookup_metrics(self, *, node: str):  # type: ignore[no-untyped-def]
        self.calls.append(ActionType.METRIC_LOOKUP)
        return await self.registry.lookup_metrics(node=node)  # type: ignore[arg-type]

    async def lookup_schema(self, *, node: str):  # type: ignore[no-untyped-def]
        self.calls.append(ActionType.SCHEMA_LOOKUP)
        return await self.registry.lookup_schema(node=node)  # type: ignore[arg-type]

    async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
        typed_action = cast(AgentAction, action)
        self.calls.append(typed_action.action_type)
        return await self.registry.invoke(typed_action, node=node)


class RecordingModel:
    def __init__(self, scripts: AgentScripts) -> None:
        self.delegate = ScriptedAgentModel(scripts)
        self.calls: list[StructuredModelRequest] = []

    @property
    def model(self) -> str:
        return self.delegate.model

    async def invoke(self, request, output_type):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        return await self.delegate.invoke(request, output_type)


class DynamicSynthesisModel(RecordingModel):
    def __init__(
        self,
        scripts: AgentScripts,
        synthesis_factory: SynthesisFactory,
    ) -> None:
        super().__init__(scripts)
        self.synthesis_factory = synthesis_factory

    async def invoke(self, request, output_type):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        if request.purpose != "synthesis":
            return await self.delegate.invoke(request, output_type)
        output = output_type.model_validate(self.synthesis_factory(request))
        return StructuredModelResult(
            output=output,
            provider_model=self.model,
            usage=ModelUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


class FailingModel:
    def __init__(self, category: AgentModelErrorCategory) -> None:
        self.category = category
        self.calls = 0

    @property
    def model(self) -> str:
        return "fixture-agent"

    async def invoke(self, request, output_type):  # type: ignore[no-untyped-def]
        del request, output_type
        self.calls += 1
        raise AgentModelError(self.category, provider_model=self.model)


class SoftAfterExecuteBudget:
    def __init__(self, delegate: BudgetLedger) -> None:
        self.delegate = delegate
        self.soft = False

    @property
    def snapshot(self) -> GovernanceSnapshot:
        return self.delegate.snapshot.model_copy(update={"soft_cap_reached": self.soft})

    def reserve_model_call(self, request):  # type: ignore[no-untyped-def]
        return self.delegate.reserve_model_call(request)

    def settle_model_call(self, reservation, usage, provider_model):  # type: ignore[no-untyped-def]
        self.delegate.settle_model_call(reservation, usage, provider_model)
        return self.snapshot

    def fail_model_call(self, reservation):  # type: ignore[no-untyped-def]
        self.delegate.fail_model_call(reservation)
        return self.snapshot

    def consume_tool(self, action_type):  # type: ignore[no-untyped-def]
        self.delegate.consume_tool(action_type)
        if action_type is ActionType.EXECUTE_SQL:
            self.soft = True
        return self.snapshot

    def ensure_action_loop_available(self) -> None:
        self.delegate.ensure_action_loop_available()

    def consume_action_loop(self) -> GovernanceSnapshot:
        self.delegate.consume_action_loop()
        return self.snapshot

    def consume_repair(self, *, structured_output: bool = False) -> GovernanceSnapshot:
        self.delegate.consume_repair(structured_output=structured_output)
        return self.snapshot

    def ensure_time_remaining(self) -> None:
        self.delegate.ensure_time_remaining()


def pricing(*, input_price: str = "0", output_price: str = "0") -> ModelPricing:
    return ModelPricing.model_validate(
        {
            "provider": "fixture",
            "region": "local",
            "requested_model": "fixture-agent",
            "resolved_model": "fixture-agent",
            "effective_date": date(2026, 9, 1),
            "currency": "CNY",
            "unit_tokens": 1000,
            "input_token_upper_bound": 1_000_000,
            "input_price": input_price,
            "output_price": output_price,
            "pricing_basis": "test",
            "source": "https://example.test/pricing",
        }
    )


def ledger(
    *,
    max_llm_calls: int = 8,
    max_tool_calls: int = 12,
    soft_cost: str = "0.20",
    hard_cost: str = "0.30",
    model_pricing: ModelPricing | None = None,
    max_repairs: int = 1,
) -> BudgetLedger:
    return BudgetLedger(
        limits=BudgetLimits(
            max_action_loops=4,
            max_llm_calls=max_llm_calls,
            max_tool_calls=max_tool_calls,
            max_execute_calls=5,
            max_profile_calls=2,
            max_repairs=max_repairs,
            timeout_seconds=60,
            soft_cost_cny=Decimal(soft_cost),
            hard_cost_cny=Decimal(hard_cost),
        ),
        pricing=model_pricing or pricing(),
        monotonic=lambda: 0.0,
    )


def execute_behavior() -> dict[str, object]:
    return {
        "action": "execute",
        "reason_code": "ready",
        "missing_fields": [],
        "user_message": "开始分析。",
    }


def simple_plan() -> dict[str, object]:
    return {
        "plan_id": "simple-plan",
        "revision": 1,
        "metric_id": "gmv",
        "metric_version": "1.0.0",
        "analysis_type": "simple",
        "windows": [
            {
                "label": "current",
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-07-01T00:00:00Z",
            }
        ],
        "null_policy": "preserve",
        "zero_denominator_policy": "return_null",
        "fill_policy": "none",
        "hypotheses": [{"hypothesis_id": "metric_value", "kind": "metric_value"}],
    }


def attribution_plan() -> dict[str, object]:
    return {
        "plan_id": "gmv-attribution-plan",
        "revision": 1,
        "metric_id": "gmv",
        "metric_version": "1.0.0",
        "analysis_type": "attribution",
        "windows": [
            {
                "label": "previous",
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-06-08T00:00:00Z",
            },
            {
                "label": "current",
                "start_at": "2026-06-08T00:00:00Z",
                "end_at": "2026-06-15T00:00:00Z",
            },
        ],
        "dimensions": ["region", "product", "segment"],
        "null_policy": "preserve",
        "zero_denominator_policy": "return_null",
        "fill_policy": "none",
        "sort": [{"column": "gmv_loss", "direction": "desc"}],
        "top_k": 10,
        "tie_break": ["region"],
        "hypotheses": [
            {"hypothesis_id": "confirm_decline", "kind": "confirm_decline"},
            {
                "hypothesis_id": "region_contribution",
                "kind": "dimension_contribution",
                "dimension": "region",
            },
            {
                "hypothesis_id": "sku_contribution",
                "kind": "dimension_contribution",
                "dimension": "product",
            },
            {
                "hypothesis_id": "segment_contribution",
                "kind": "dimension_contribution",
                "dimension": "segment",
            },
        ],
    }


def execute_action(
    contract_id: str = "metric_value_contract",
    hypothesis_id: str = "metric_value",
    *,
    sql: str = "select cast(125 as numeric) as gmv",
) -> dict[str, object]:
    return {
        "action_type": "execute_sql",
        "purpose": contract_id,
        "arguments": {"sql": sql},
        "hypothesis_id": hypothesis_id,
        "contract_id": contract_id,
        "expected_evidence": "contracted numeric result",
    }


def profile_action() -> dict[str, object]:
    return {
        "action_type": "profile",
        "purpose": "profile private_filter_value_sentinel",
        "arguments": {
            "table_name": "orders",
            "column_name": "region",
            "operation": "top_values",
            "filters": [{"column_name": "status", "value": "private_filter_value_sentinel"}],
            "limit": 5,
        },
        "hypothesis_id": "metric_value",
        "contract_id": None,
        "expected_evidence": "profile_context",
    }


def synthesis(*, evidence_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "status": "completed",
        "stop_reason": "answer_complete",
        "answer": "GMV 分析已完成。",
        "evidence_ids": evidence_ids or [],
    }


def scripts_for(
    query: str,
    *,
    behavior: dict[str, object] | None = None,
    plan: dict[str, object] | None = None,
    actions: Sequence[dict[str, object]] = (),
    synthesis_output: dict[str, object] | None = None,
    repairs: Sequence[dict[str, object]] = (),
) -> AgentScripts:
    purposes: dict[ModelPurpose, Sequence[Mapping[str, object]]] = {
        "behavior": (behavior or execute_behavior(),),
    }
    if plan is not None:
        purposes["plan"] = (plan,)
    if actions:
        purposes["action"] = tuple(actions)
    if synthesis_output is not None:
        purposes["synthesis"] = (synthesis_output,)
    if repairs:
        purposes["repair"] = tuple(repairs)
    return cast(AgentScripts, {query.strip().casefold(): purposes})


def query_result(
    columns: tuple[str, ...] = ("gmv",),
    rows: tuple[tuple[object, ...], ...] = (("125.00",),),
    *,
    query_id: str = QUERY_ID,
    possibly_truncated: bool = False,
) -> QueryResult:
    return QueryResult(
        query_id=query_id,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        possibly_truncated=possibly_truncated,
    )


def context_for(
    scripts: AgentScripts,
    results: Sequence[QueryResult],
    *,
    budget: object | None = None,
    metric_tool: MetricTool | None = None,
) -> tuple[AgentContext, RecordingTools, RecordingModel, RecordingEvents, SequenceBackend]:
    backend = SequenceBackend(results)
    profile = RecordingProfileTool()
    registry = ToolRegistry.default(
        SchemaTool(),
        metric_tool or MetricTool(),
        profile,
        ExecuteSqlTool(backend=backend),
    )
    tools = RecordingTools(registry)
    model = RecordingModel(scripts)
    effective_budget = budget or ledger()
    recorder = InMemoryTraceRecorder()
    clock = FrozenClock()
    invoker = StructuredModelInvoker(
        model,
        effective_budget,  # type: ignore[arg-type]
        recorder,
        clock,
    )
    events = RecordingEvents()
    context = AgentContext(
        model_invoker=invoker,
        tools=tools,
        budget=effective_budget,  # type: ignore[arg-type]
        events=events,
        trace_recorder=recorder,
        clock=clock,
    )
    return context, tools, model, events, backend


def replace_context(
    context: AgentContext,
    *,
    model: object | None = None,
    tools: object | None = None,
    events: object | None = None,
    recorder: InMemoryTraceRecorder | None = None,
) -> AgentContext:
    effective_recorder = recorder or cast(InMemoryTraceRecorder, context.trace_recorder)
    invoker = context.model_invoker
    if model is not None:
        invoker = StructuredModelInvoker(
            model,  # type: ignore[arg-type]
            context.budget,
            effective_recorder,
            context.clock,
        )
    return AgentContext(
        model_invoker=invoker,
        tools=tools or context.tools,  # type: ignore[arg-type]
        budget=context.budget,
        events=events or context.events,  # type: ignore[arg-type]
        trace_recorder=effective_recorder,
        clock=context.clock,
    )


def evidence_ids_from_request(request: StructuredModelRequest) -> tuple[str, ...]:
    raw_evidence = request.user_payload["evidence"]
    assert isinstance(raw_evidence, tuple)
    return tuple(
        cast(str, item["evidence_id"]) for item in raw_evidence if isinstance(item, Mapping)
    )


@pytest.mark.asyncio
async def test_clarify_terminates_without_tool_calls() -> None:
    scripts = scripts_for(
        CLARIFY_QUERY,
        behavior={
            "action": "clarify",
            "reason_code": "missing_time_window",
            "missing_fields": ["time_window"],
            "user_message": "请补充查询时间范围。",
        },
    )
    context, tools, model, _, _ = context_for(scripts, ())

    result = await run_agent(run_id="clarify-1", query=CLARIFY_QUERY, context=context)

    assert result.final_answer.status is FinalStatus.CLARIFICATION_REQUIRED
    assert result.final_answer.stop_reason is StopReason.MISSING_REQUIRED_FIELDS
    assert tools.calls == []
    assert [call.purpose for call in model.calls] == ["behavior"]


@pytest.mark.asyncio
async def test_simple_metric_runs_real_registry_path_once() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, tools, _, _, _ = context_for(scripts, (query_result(),))

    result = await run_agent(run_id="simple-1", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.COMPLETED
    assert tuple(to.value for to in tools.calls) == (
        "metric_lookup",
        "schema_lookup",
        "execute_sql",
    )
    assert result.evidence[0].query_id == QUERY_ID
    assert set(result.final_answer.evidence_ids) == {
        item.evidence_id for item in result.evidence if item.verified
    }
    assert result.governance.action_loops == 1
    assert result.governance.llm_calls == 4
    assert result.governance.tool_calls == 3
    assert result.governance.execute_calls == 1
    assert result.governance.repair_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension_rows", [1, 10])
async def test_attribution_checks_decline_then_three_dimensions(dimension_rows: int) -> None:
    actions = (
        execute_action(
            "gmv_comparison",
            "confirm_decline",
            sql=(
                "select 80::numeric as current_gmv, 100::numeric as previous_gmv, "
                "-0.2::numeric as change_rate"
            ),
        ),
        execute_action(
            "region_contribution",
            "region_contribution",
            sql="select 'north' as region, 12::numeric as gmv_loss",
        ),
        execute_action(
            "sku_contribution",
            "sku_contribution",
            sql="select 'sku-1' as sku, 9::numeric as gmv_loss",
        ),
        execute_action(
            "segment_contribution",
            "segment_contribution",
            sql=(
                "select 'vip' as segment, 100::numeric as previous_gmv, "
                "80::numeric as current_gmv, -20::numeric as delta"
            ),
        ),
    )
    results = (
        query_result(
            ("current_gmv", "previous_gmv", "change_rate"),
            (("80", "100", "-0.2"),),
            query_id="1" * 64,
        ),
        query_result(
            ("region", "gmv_loss"),
            tuple((f"region-{i:02d}", str(20 - i)) for i in range(dimension_rows)),
            query_id="2" * 64,
        ),
        query_result(
            ("sku", "gmv_loss"),
            tuple((f"sku-{i:02d}", str(20 - i)) for i in range(dimension_rows)),
            query_id="3" * 64,
        ),
        query_result(
            ("segment", "previous_gmv", "current_gmv", "delta"),
            (("vip", "100", "80", "-20"),),
            query_id="4" * 64,
        ),
    )
    scripts = scripts_for(
        ATTRIBUTION_QUERY,
        plan=attribution_plan(),
        actions=actions,
        synthesis_output=synthesis(),
    )
    context, _, model, _, _ = context_for(scripts, results)

    result = await run_agent(run_id="attribution-1", query=ATTRIBUTION_QUERY, context=context)

    assert tuple(item.contract_id for item in result.observations if item.query_id) == (
        "gmv_comparison",
        "region_contribution",
        "sku_contribution",
        "segment_contribution",
    )
    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.governance.action_loops == 4
    assert result.governance.llm_calls == 7
    assert result.governance.tool_calls == 6
    assert result.governance.execute_calls == 4
    assert result.governance.repair_count == 0
    request = next(call for call in model.calls if call.purpose == "synthesis")
    assert request.max_output_tokens <= 4096
    if dimension_rows == 10:
        assert len(evidence_ids_from_request(request)) > 20
        assert request.max_output_tokens >= len(json.dumps(evidence_ids_from_request(request)))


@pytest.mark.asyncio
async def test_repairable_column_contract_failure_repairs_once() -> None:
    repaired = execute_action(sql="select cast(125 as numeric) as gmv")
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="select cast(125 as numeric) as value"),),
        synthesis_output=synthesis(),
        repairs=(repaired,),
    )
    context, tools, model, events, backend = context_for(
        scripts,
        (
            query_result(("value",), (("125",),), query_id="c" * 64),
            query_result(("gmv",), (("125",),), query_id="d" * 64),
        ),
    )

    result = await run_agent(run_id="repair-1", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.governance.repair_count == 1
    assert result.governance.action_loops == 1
    assert result.governance.llm_calls == 5
    assert result.governance.tool_calls == 4
    assert result.governance.execute_calls == 2
    assert tools.calls.count(ActionType.EXECUTE_SQL) == backend.calls == 2
    assert [call.purpose for call in model.calls].count("repair") == 1
    assert result.first_candidate is not None
    assert result.first_candidate.columns == ("value",)
    repair_request = next(call for call in model.calls if call.purpose == "repair")
    repair_payload = repair_request.model_dump(mode="json")["user_payload"]
    assert repair_payload["query"] == QUERY
    assert repair_payload["plan"]["metric_id"] == "gmv"
    assert any(item["metric_id"] == "gmv" for item in repair_payload["metrics"])
    assert any(item["name"] == "orders" for item in repair_payload["tables"])
    assert repair_payload["execute_arguments_schema"]["properties"]["parameters"]
    assert result.observation_validations[0].error_code == "column_contract_mismatch"
    assert result.observation_validations[0].repairable is True
    repair_events = [item for item in events.items if item[1].startswith("repair.")]
    assert [item[1] for item in repair_events] == ["repair.started", "repair.completed"]
    assert all(set(item[2]) == {"repair_count", "error_code", "success"} for item in repair_events)


@pytest.mark.asyncio
async def test_second_contract_failure_stops_without_third_execute() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="select cast(125 as numeric) as value"),),
        repairs=(execute_action(sql="select cast(125 as numeric) as still_wrong"),),
    )
    context, tools, _, _, backend = context_for(
        scripts,
        (
            query_result(("value",), (("125",),), query_id="c" * 64),
            query_result(("still_wrong",), (("125",),), query_id="d" * 64),
        ),
    )

    result = await run_agent(run_id="repair-2", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.EXECUTION_FAILED
    assert result.final_answer.stop_reason is StopReason.REPAIR_FAILED
    assert result.governance.repair_count == 1
    assert result.governance.action_loops == 1
    assert result.governance.llm_calls == 4
    assert result.governance.tool_calls == 4
    assert result.governance.execute_calls == 2
    assert tools.calls.count(ActionType.EXECUTE_SQL) == backend.calls == 2
    assert tuple(item.error_code for item in result.observation_validations) == (
        "column_contract_mismatch",
        "column_contract_mismatch",
    )


@pytest.mark.asyncio
async def test_nonrepairable_failure_consumes_one_loop_without_repair() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="drop table orders"),),
    )
    context, tools, _, events, backend = context_for(scripts, ())

    result = await run_agent(run_id="policy-1", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.POLICY_BLOCKED
    assert result.final_answer.stop_reason is StopReason.SQL_POLICY_REJECTED
    assert result.governance.action_loops == 1
    assert result.governance.llm_calls == 3
    assert result.governance.tool_calls == 3
    assert result.governance.execute_calls == 1
    assert result.governance.repair_count == 0
    assert tools.calls.count(ActionType.EXECUTE_SQL) == 1
    assert backend.calls == 0
    failed = next(item for item in events.items if item[1] == "tool.failed")
    assert set(failed[2]) == {"tool_name", "purpose", "safe_error"}


@pytest.mark.asyncio
async def test_repair_execute_reapplies_full_sql_policy_without_backend_call() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(sql="drop table orders"),),
    )
    context, tools, _, _, backend = context_for(
        scripts,
        (query_result(("gmv",), (("not-a-number",),), query_id="c" * 64),),
    )

    result = await run_agent(run_id="repair-policy", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.POLICY_BLOCKED
    assert result.final_answer.stop_reason is StopReason.SQL_POLICY_REJECTED
    assert result.governance.action_loops == 1
    assert result.governance.llm_calls == 4
    assert result.governance.tool_calls == 4
    assert result.governance.execute_calls == 2
    assert result.governance.repair_count == 1
    assert tools.calls.count(ActionType.EXECUTE_SQL) == 2
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_repair_output_invalid_stops_without_second_execute() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=({"not": "an action"},),
    )
    context, tools, model, _, backend = context_for(
        scripts,
        (query_result(("gmv",), (("not-a-number",),), query_id="c" * 64),),
    )

    result = await run_agent(run_id="repair-invalid", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.EXECUTION_FAILED
    assert result.final_answer.stop_reason is StopReason.REPAIR_FAILED
    assert result.governance.repair_count == 1
    assert result.governance.action_loops == 1
    assert result.governance.llm_calls == 4
    assert result.governance.tool_calls == 3
    assert result.governance.execute_calls == 1
    assert tools.calls.count(ActionType.EXECUTE_SQL) == backend.calls == 1
    assert [call.purpose for call in model.calls].count("repair") == 1


@pytest.mark.asyncio
async def test_profile_context_can_precede_execute_without_contract_id_or_evidence() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(profile_action(), execute_action()),
        synthesis_output=synthesis(),
    )
    context, tools, _, _, _ = context_for(scripts, (query_result(),))

    result = await run_agent(run_id="profile-1", query=QUERY, context=context)

    profile = next(item for item in result.observations if item.tool_name is ActionType.PROFILE)
    assert profile.contract_id is None
    assert not any(item.observation_id == profile.observation_id for item in result.evidence)
    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.governance.profile_calls == 1
    assert result.governance.llm_calls == 5
    assert result.governance.tool_calls == 4
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert tuple(item.value for item in tools.calls) == (
        "metric_lookup",
        "schema_lookup",
        "profile",
        "execute_sql",
    )


@pytest.mark.asyncio
async def test_soft_cap_stops_optional_actions_and_finalizes_from_existing_evidence() -> None:
    budget = SoftAfterExecuteBudget(ledger())
    scripts = scripts_for(
        ATTRIBUTION_QUERY,
        plan=attribution_plan(),
        actions=(
            execute_action(
                "gmv_comparison",
                "confirm_decline",
                sql=(
                    "select 80::numeric as current_gmv, 100::numeric as previous_gmv, "
                    "-0.2::numeric as change_rate"
                ),
            ),
            profile_action(),
        ),
        synthesis_output=synthesis(),
    )
    context, tools, model, events, _ = context_for(
        scripts,
        (
            query_result(
                ("current_gmv", "previous_gmv", "change_rate"),
                (("80", "100", "-0.2"),),
            ),
        ),
        budget=budget,
    )

    result = await run_agent(
        run_id="soft-1",
        query=ATTRIBUTION_QUERY,
        context=context,
    )

    assert result.final_answer.status is FinalStatus.PARTIAL
    assert result.final_answer.stop_reason is StopReason.EVIDENCE_PARTIAL
    assert result.evidence
    assert ActionType.PROFILE not in tools.calls
    assert [call.purpose for call in model.calls] == ["behavior", "plan", "action"]
    assert result.governance.repair_count == 0
    assert any(item[1] == "budget.warning" for item in events.items)


@pytest.mark.asyncio
async def test_hard_cap_before_any_evidence_is_budget_exhausted() -> None:
    hard_budget = ledger(
        soft_cost="0.000001",
        hard_cost="0.000002",
        model_pricing=pricing(input_price="1", output_price="1"),
    )
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, model, events, _ = context_for(scripts, (), budget=hard_budget)

    result = await run_agent(run_id="hard-1", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.BUDGET_EXHAUSTED
    assert result.final_answer.stop_reason is StopReason.COST_HARD_CAP
    assert result.governance.llm_calls == result.governance.tool_calls == 0
    assert model.calls == []
    assert tools.calls == []
    warning = next(item for item in events.items if item[1] == "budget.warning")
    assert set(warning[2]) == {
        "reason",
        "llm_calls",
        "tool_calls",
        "execute_calls",
        "profile_calls",
        "repair_count",
        "committed_cost_cny",
    }


@pytest.mark.asyncio
async def test_fifth_normal_execute_is_rejected_before_any_tool_call() -> None:
    budget = ledger()
    for _ in range(4):
        budget.consume_action_loop()
    scripts = scripts_for(QUERY, actions=(execute_action(),))
    context, tools, model, events, backend = context_for(scripts, (), budget=budget)
    plan = TypedMetricPlan.model_validate(simple_plan())
    metric = next(item for item in MetricTool().list().data or () if item.metric_id == "gmv")
    state = new_agent_state(run_id="loop-limit", query=QUERY)
    state["plan_revisions"] = (plan,)
    state["answer_contract"] = compile_answer_contract(plan, metric)

    delta = await route_action(state, Runtime(context=context))

    assert delta["stop_reason"] is StopReason.ANALYSIS_LOOP_LIMIT
    assert delta["next_action"] is None
    assert budget.snapshot.action_loops == 4
    assert budget.snapshot.llm_calls == 1
    assert [call.purpose for call in model.calls] == ["action"]
    assert tools.calls == []
    assert backend.calls == 0
    assert any(item[1] == "budget.warning" for item in events.items)


@pytest.mark.asyncio
async def test_synthesis_without_budget_uses_deterministic_finalize() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, model, _, _ = context_for(
        scripts, (query_result(),), budget=ledger(max_llm_calls=3)
    )

    result = await run_agent(run_id="no-synthesis-budget", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.final_answer.stop_reason is StopReason.ANSWER_COMPLETE
    assert result.final_answer.evidence_ids
    assert [call.purpose for call in model.calls] == ["behavior", "plan", "action"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["   ", "x" * 4097])
async def test_invalid_query_stops_at_intake_without_model_or_tool_calls(query: str) -> None:
    context, tools, model, _, _ = context_for({}, ())

    result = await run_agent(run_id="invalid-query", query=query, context=context)

    assert result.final_answer.status is FinalStatus.CLARIFICATION_REQUIRED
    assert result.final_answer.stop_reason is StopReason.MISSING_REQUIRED_FIELDS
    assert result.governance.llm_calls == result.governance.tool_calls == 0
    assert model.calls == []
    assert tools.calls == []


@pytest.mark.asyncio
async def test_context_failure_stops_before_schema_without_fabricating_observation() -> None:
    class FailedMetricTool(MetricTool):
        def list(self) -> ToolResponse[tuple[MetricInfo, ...]]:
            return ToolResponse(
                ok=False,
                error=ToolError(
                    code=ErrorCode.EXECUTION_FAILED,
                    message="private raw context failure",
                ),
            )

    scripts = scripts_for(QUERY)
    context, tools, model, _, _ = context_for(scripts, (), metric_tool=FailedMetricTool())

    result = await run_agent(run_id="context-failure", query=QUERY, context=context)

    assert tools.calls == [ActionType.METRIC_LOOKUP]
    assert tuple(item.tool_name for item in result.observations) == (ActionType.METRIC_LOOKUP,)
    assert [call.purpose for call in model.calls] == ["behavior"]
    assert result.final_answer.status is FinalStatus.EXECUTION_FAILED


@pytest.mark.asyncio
async def test_context_budget_failure_after_metric_does_not_fabricate_schema() -> None:
    scripts = scripts_for(QUERY)
    context, tools, model, events, _ = context_for(
        scripts,
        (),
        budget=ledger(max_tool_calls=1),
    )

    result = await run_agent(run_id="context-budget", query=QUERY, context=context)

    assert tools.calls == [ActionType.METRIC_LOOKUP]
    assert tuple(item.tool_name for item in result.observations) == (ActionType.METRIC_LOOKUP,)
    assert [call.purpose for call in model.calls] == ["behavior"]
    assert result.final_answer.status is FinalStatus.BUDGET_EXHAUSTED
    assert result.final_answer.stop_reason is StopReason.TOOL_CALL_LIMIT
    assert any(item[1] == "budget.warning" for item in events.items)


@pytest.mark.asyncio
async def test_graph_emits_only_safe_ordered_domain_events() -> None:
    row_sentinel = "private_row_sentinel"
    sql_sentinel = "private_sql_sentinel"
    event_plan = simple_plan()
    event_plan["dimensions"] = ["region"]
    scripts = scripts_for(
        QUERY,
        plan=event_plan,
        actions=(
            execute_action(
                sql=(
                    f"select '{row_sentinel}' as region, cast(125 as numeric) as gmv "
                    f"/* {sql_sentinel} */"
                )
            ),
        ),
        synthesis_output=synthesis(),
    )
    context, _, _, events, _ = context_for(
        scripts,
        (query_result(("region", "gmv"), ((row_sentinel, "125.00"),)),),
    )

    await run_agent(run_id="events-1", query=QUERY, context=context)

    assert [item[1] for item in events.items] == [
        "behavior.decided",
        "context.retrieved",
        "plan.created",
        "tool.started",
        "tool.completed",
        "observation.validated",
        "hypothesis.updated",
        "evidence.assessed",
    ]
    expected_keys = {
        "behavior.decided": {"action", "reason_code", "missing_fields"},
        "context.retrieved": {"metric_count", "table_count", "success"},
        "plan.created": {
            "plan_id",
            "revision",
            "analysis_type",
            "metric_id",
            "hypothesis_ids",
        },
        "tool.started": {"tool_name", "purpose", "contract_id"},
        "tool.completed": {
            "tool_name",
            "purpose",
            "query_id",
            "columns",
            "row_count",
            "possibly_truncated",
        },
        "observation.validated": {"contract_id", "valid", "error_code", "repairable"},
        "hypothesis.updated": {"hypothesis_id", "status"},
        "evidence.assessed": {"verified_count", "gaps", "partial", "complete"},
    }
    for _, event_type, data in events.items:
        assert set(data) == expected_keys[event_type]
    serialized = json.dumps(events.items, ensure_ascii=False)
    for sentinel in (
        row_sentinel,
        sql_sentinel,
        "private_filter_value_sentinel",
        "parameters",
        "prompt",
        "api_key",
        "endpoint",
        "rows",
        "payload",
    ):
        assert sentinel not in serialized.lower()
    assert not ({"run.created", "run.started", "run.terminal"} & {x[1] for x in events.items})


@pytest.mark.asyncio
async def test_trace_recorder_and_graph_state_histories_are_identical() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, _, _, _ = context_for(scripts, (query_result(),))
    graph = build_agent_graph()

    state = await graph.ainvoke(new_agent_state(run_id="trace-1", query=QUERY), context=context)
    safe_trace = context.trace_recorder.snapshot()

    assert state["node_traces"] == safe_trace.nodes
    assert state["model_call_traces"] == safe_trace.model_calls
    assert state["tool_call_traces"] == safe_trace.tool_calls
    assert graph.checkpointer is None
    assert graph.store is None


@pytest.mark.asyncio
async def test_forged_synthesis_evidence_reference_fails_closed() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(evidence_ids=["forged-evidence"]),
    )
    context, _, _, _, _ = context_for(scripts, (query_result(),))

    result = await run_agent(run_id="forged-synthesis", query=QUERY, context=context)

    verified = {item.evidence_id for item in result.evidence if item.verified}
    assert set(result.final_answer.evidence_ids).issubset(verified)
    assert "forged-evidence" not in result.final_answer.evidence_ids


@pytest.mark.asyncio
async def test_synthesis_final_answer_rejects_sql_secret_endpoint_and_raw_row_text() -> None:
    unsafe_answer = (
        "select private_row_sentinel from orders; sk-private-key https://private-endpoint.invalid"
    )
    unsafe_synthesis = synthesis()
    unsafe_synthesis["answer"] = unsafe_answer
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=unsafe_synthesis,
    )
    context, _, _, _, _ = context_for(scripts, (query_result(),))

    result = await run_agent(run_id="unsafe-synthesis", query=QUERY, context=context)

    rendered = result.final_answer.model_dump_json().lower()
    for sentinel in (
        "select ",
        "private_row_sentinel",
        "sk-private-key",
        "private-endpoint.invalid",
    ):
        assert sentinel not in rendered
    assert set(result.final_answer.evidence_ids).issubset(
        {item.evidence_id for item in result.evidence if item.verified}
    )


@pytest.mark.asyncio
async def test_unknown_node_failure_is_mapped_to_internal_error_terminal() -> None:
    class ExplodingTools(RecordingTools):
        async def lookup_metrics(self, *, node: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("raw exception sentinel")

    scripts = scripts_for(QUERY)
    context, _, _, events, _ = context_for(scripts, ())
    exploding = ExplodingTools(cast(RecordingTools, context.tools).registry)
    context = AgentContext(
        model_invoker=context.model_invoker,
        tools=exploding,
        budget=context.budget,
        events=context.events,
        trace_recorder=context.trace_recorder,
        clock=context.clock,
    )

    result = await run_agent(run_id="unknown-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR
    assert "raw exception sentinel" not in json.dumps(
        {
            "answer": result.final_answer.model_dump(mode="json"),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )


@pytest.mark.asyncio
async def test_unknown_contract_compiler_failure_is_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object) -> object:
        raise RuntimeError("raw compiler exception sentinel")

    monkeypatch.setattr(
        "governed_analytics.agent.nodes.planning.compile_answer_contract",
        explode,
    )
    scripts = scripts_for(QUERY, plan=simple_plan())
    context, _, _, _, _ = context_for(scripts, ())

    result = await run_agent(run_id="compiler-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR
    assert "raw compiler exception sentinel" not in result.final_answer.model_dump_json()


def test_central_failure_mapper_uses_stable_policy_timeout_and_unknown_reasons() -> None:
    assert map_agent_failure(SqlPolicyError(SqlRejectionCode.NOT_READONLY_QUERY)) is (
        StopReason.SQL_POLICY_REJECTED
    )
    assert map_agent_failure(TimeoutError("raw timeout sentinel")) is StopReason.INTERNAL_ERROR
    assert map_agent_failure(RuntimeError("raw exception sentinel")) is StopReason.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_500_row_truncated_result_returns_legal_partial_terminal() -> None:
    rows = tuple((str(index),) for index in range(500))
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
    )
    context, _, _, _, backend = context_for(
        scripts,
        (query_result(rows=rows, possibly_truncated=True),),
    )

    result = await run_agent(run_id="truncated-500", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.PARTIAL
    assert result.final_answer.stop_reason is StopReason.RESULT_TRUNCATED
    assert result.final_answer.evidence_ids == ()
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_result_truncated_preserves_reason_with_existing_verified_evidence() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, _, _, _ = context_for(scripts, (query_result(),))
    graph = build_agent_graph()
    state = cast(
        AgentState,
        await graph.ainvoke(
            new_agent_state(run_id="truncated-with-evidence", query=QUERY),
            context=context,
        ),
    )
    state["final_answer"] = None
    state["stop_reason"] = StopReason.RESULT_TRUNCATED

    delta = await finalize(state, Runtime(context=context))

    answer = delta["final_answer"]
    assert isinstance(answer, FinalAnswer)
    assert answer.status is FinalStatus.PARTIAL
    assert answer.stop_reason is StopReason.RESULT_TRUNCATED
    assert answer.evidence_ids == tuple(item.evidence_id for item in state["evidence"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [StopReason.SQL_TIMEOUT, StopReason.TASK_TIMEOUT])
async def test_timeout_without_evidence_is_execution_failed_and_preserves_reason(
    reason: StopReason,
) -> None:
    context, _, _, _, _ = context_for(scripts_for(QUERY), ())
    state = new_agent_state(run_id=f"no-evidence-{reason.value}", query=QUERY)
    state["stop_reason"] = reason

    delta = await finalize(state, Runtime(context=context))

    answer = delta["final_answer"]
    assert isinstance(answer, FinalAnswer)
    assert answer.status is FinalStatus.EXECUTION_FAILED
    assert answer.stop_reason is reason


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [StopReason.SQL_TIMEOUT, StopReason.TASK_TIMEOUT])
async def test_timeout_with_verified_evidence_is_partial_and_preserves_reason(
    reason: StopReason,
) -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, _, _, _ = context_for(scripts, (query_result(),))
    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id=f"evidence-{reason.value}", query=QUERY),
            context=context,
        ),
    )
    state["final_answer"] = None
    state["stop_reason"] = reason

    delta = await finalize(state, Runtime(context=context))

    answer = delta["final_answer"]
    assert isinstance(answer, FinalAnswer)
    assert answer.status is FinalStatus.PARTIAL
    assert answer.stop_reason is reason
    assert answer.evidence_ids == tuple(item.evidence_id for item in state["evidence"])


@pytest.mark.asyncio
async def test_select_star_is_output_policy_blocked_before_backend() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="select * from orders"),),
    )
    context, _, _, events, backend = context_for(scripts, ())

    result = await run_agent(run_id="select-star", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.POLICY_BLOCKED
    assert result.final_answer.stop_reason is StopReason.SQL_POLICY_REJECTED
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert backend.calls == 0
    failed = next(item for item in events.items if item[1] == "tool.failed")
    assert failed[2]["safe_error"] == "output_shape_policy"


@pytest.mark.asyncio
async def test_nonfinite_result_projection_is_output_policy_blocked() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
    )
    context, _, _, events, backend = context_for(
        scripts,
        (cast(QueryResult, object()),),
    )

    result = await run_agent(run_id="projection-policy", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.POLICY_BLOCKED
    assert result.final_answer.stop_reason is StopReason.SQL_POLICY_REJECTED
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert backend.calls == 1
    assert (
        next(item for item in events.items if item[1] == "tool.failed")[2]["safe_error"]
        == "output_shape_policy"
    )


@pytest.mark.asyncio
async def test_compiler_timeout_is_internal_not_sql_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object) -> object:
        raise TimeoutError("raw compiler timeout sentinel")

    monkeypatch.setattr(
        "governed_analytics.agent.nodes.planning.compile_answer_contract",
        timeout,
    )
    context, _, _, _, _ = context_for(scripts_for(QUERY, plan=simple_plan()), ())

    result = await run_agent(run_id="compiler-timeout", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "expected_status", "expected_reason"),
    [
        (
            {
                "action": "refuse",
                "reason_code": "unsafe_request",
                "missing_fields": [],
                "user_message": "拒绝。",
            },
            FinalStatus.REFUSED,
            StopReason.UNSAFE_REQUEST,
        ),
        (
            {
                "action": "unsupported",
                "reason_code": "unsupported_analysis",
                "missing_fields": [],
                "user_message": "不支持。",
            },
            FinalStatus.UNSUPPORTED,
            StopReason.UNSUPPORTED_ANALYSIS,
        ),
    ],
)
async def test_refuse_and_unsupported_never_call_tools(
    behavior: dict[str, object],
    expected_status: FinalStatus,
    expected_reason: StopReason,
) -> None:
    context, tools, model, _, _ = context_for(
        scripts_for(QUERY, behavior=behavior),
        (),
    )

    result = await run_agent(run_id="behavior-short", query=QUERY, context=context)

    assert result.final_answer.status is expected_status
    assert result.final_answer.stop_reason is expected_reason
    assert tools.calls == []
    assert [call.purpose for call in model.calls] == ["behavior"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "expected_status", "expected_reason"),
    [
        (
            {
                "action": "clarify",
                "reason_code": "missing_time_window",
                "missing_fields": ["time_window"],
                "user_message": "请补充查询时间范围。",
            },
            FinalStatus.CLARIFICATION_REQUIRED,
            StopReason.MISSING_REQUIRED_FIELDS,
        ),
        (
            {
                "action": "refuse",
                "reason_code": "unsafe_request",
                "missing_fields": [],
                "user_message": "拒绝。",
            },
            FinalStatus.REFUSED,
            StopReason.UNSAFE_REQUEST,
        ),
        (
            {
                "action": "unsupported",
                "reason_code": "unsupported_analysis",
                "missing_fields": [],
                "user_message": "不支持。",
            },
            FinalStatus.UNSUPPORTED,
            StopReason.UNSUPPORTED_ANALYSIS,
        ),
    ],
)
async def test_soft_cap_does_not_replace_terminal_behavior_decision(
    behavior: dict[str, object],
    expected_status: FinalStatus,
    expected_reason: StopReason,
) -> None:
    budget = SoftAfterExecuteBudget(ledger())
    budget.soft = True
    context, tools, model, events, _ = context_for(
        scripts_for(QUERY, behavior=behavior),
        (),
        budget=budget,
    )

    result = await run_agent(run_id="soft-terminal-behavior", query=QUERY, context=context)

    assert result.final_answer.status is expected_status
    assert result.final_answer.stop_reason is expected_reason
    assert result.governance.soft_cap_reached is True
    assert tools.calls == []
    assert [call.purpose for call in model.calls] == ["behavior"]
    assert [item[1] for item in events.items].count("budget.warning") == 1


@pytest.mark.asyncio
async def test_soft_cap_stops_execute_behavior_before_downstream_calls() -> None:
    budget = SoftAfterExecuteBudget(ledger())
    budget.soft = True
    context, tools, model, events, _ = context_for(
        scripts_for(QUERY),
        (),
        budget=budget,
    )

    result = await run_agent(run_id="soft-execute-behavior", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.BUDGET_EXHAUSTED
    assert result.final_answer.stop_reason is StopReason.COST_SOFT_CAP
    assert result.governance.soft_cap_reached is True
    assert tools.calls == []
    assert [call.purpose for call in model.calls] == ["behavior"]
    assert [item[1] for item in events.items].count("budget.warning") == 1


@pytest.mark.asyncio
async def test_premise_not_met_has_completed_terminal_and_stops_at_comparison() -> None:
    scripts = scripts_for(
        ATTRIBUTION_QUERY,
        plan=attribution_plan(),
        actions=(
            execute_action(
                "gmv_comparison",
                "confirm_decline",
                sql=(
                    "select 100::numeric as current_gmv, 80::numeric as previous_gmv, "
                    "0.25::numeric as change_rate"
                ),
            ),
        ),
        synthesis_output={
            "status": "completed",
            "stop_reason": "premise_not_met",
            "answer": "前提不成立。",
            "evidence_ids": [],
            "completed_dimensions": ["confirm_decline"],
        },
    )
    context, tools, _, events, _ = context_for(
        scripts,
        (
            query_result(
                ("current_gmv", "previous_gmv", "change_rate"),
                (("100", "80", "0.25"),),
            ),
        ),
    )

    result = await run_agent(run_id="premise-not-met", query=ATTRIBUTION_QUERY, context=context)

    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.final_answer.stop_reason is StopReason.PREMISE_NOT_MET
    assert result.final_answer.completed_dimensions == ("confirm_decline",)
    assert tools.calls.count(ActionType.EXECUTE_SQL) == 1
    assert [item[1] for item in events.items][-2:] == [
        "hypothesis.updated",
        "evidence.assessed",
    ]


@pytest.mark.asyncio
async def test_second_invalid_repair_records_and_emits_failure() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
    )
    context, _, _, events, _ = context_for(
        scripts,
        (
            query_result(("gmv",), (("not-a-number",),), query_id="c" * 64),
            query_result(("gmv",), (("still-not-a-number",),), query_id="d" * 64),
        ),
    )

    result = await run_agent(run_id="repair-audit-failed", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.REPAIR_FAILED
    assert result.repair_history[-1].outcome == "failed"
    assert result.repair_history[-1].repaired_observation_id is None
    repair_events = [item for item in events.items if item[1].startswith("repair.")]
    assert [item[2]["success"] for item in repair_events] == [False, False]
    assert [item[1] for item in events.items] == [
        "behavior.decided",
        "context.retrieved",
        "plan.created",
        "tool.started",
        "tool.completed",
        "observation.validated",
        "repair.started",
        "tool.started",
        "tool.completed",
        "observation.validated",
        "repair.completed",
    ]


@pytest.mark.asyncio
async def test_successful_repair_records_and_emits_success() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, _, events, _ = context_for(
        scripts,
        (
            query_result(("gmv",), (("not-a-number",),), query_id="c" * 64),
            query_result(query_id="d" * 64),
        ),
    )

    result = await run_agent(run_id="repair-audit-success", query=QUERY, context=context)

    assert result.repair_history[-1].outcome == "success"
    assert result.repair_history[-1].repaired_observation_id is not None
    repair_events = [item for item in events.items if item[1].startswith("repair.")]
    assert [item[2]["success"] for item in repair_events] == [False, True]
    assert [item[0] for item in repair_events] == ["repair", "repair"]
    assert [item[1] for item in events.items] == [
        "behavior.decided",
        "context.retrieved",
        "plan.created",
        "tool.started",
        "tool.completed",
        "observation.validated",
        "repair.started",
        "tool.started",
        "tool.completed",
        "observation.validated",
        "repair.completed",
        "hypothesis.updated",
        "evidence.assessed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    [
        AgentModelErrorCategory.PROVIDER_CALL_FAILED,
        AgentModelErrorCategory.INVALID_STRUCTURE,
    ],
)
async def test_model_failures_do_not_emit_budget_warning(
    category: AgentModelErrorCategory,
) -> None:
    context, _, _, events, _ = context_for({}, ())
    failing = FailingModel(category)
    context = replace_context(context, model=failing)

    result = await run_agent(run_id="model-failure", query=QUERY, context=context)

    assert result.final_answer.stop_reason in {
        StopReason.MODEL_UNAVAILABLE,
        StopReason.REPAIR_FAILED,
    }
    assert not any(item[1] == "budget.warning" for item in events.items)


@pytest.mark.asyncio
async def test_tool_port_exception_becomes_safe_failed_observation_and_consumes_loop() -> None:
    raw_sentinel = "raw tool port secret sentinel"

    class ExplodingInvokeTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            del action, node
            raise RuntimeError(raw_sentinel)

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(
        context,
        tools=ExplodingInvokeTools(tools.registry),
    )

    result = await run_agent(run_id="tool-port-failure", query=QUERY, context=context)

    failed = result.observations[-1]
    assert failed.tool_name is ActionType.EXECUTE_SQL
    assert failed.purpose == "metric_value_contract"
    assert failed.safe_error == "internal_tool_error"
    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert result.safe_trace.tool_calls[-1].safe_error == "internal_tool_error"
    rendered = json.dumps(
        {
            "answer": result.final_answer.model_dump(mode="json"),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )
    assert raw_sentinel not in rendered


@pytest.mark.asyncio
async def test_repair_tool_port_exception_is_safe_failed_repair() -> None:
    raw_sentinel = "raw repair port secret sentinel"

    class ExplodingRepairTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            if node == "repair":
                raise RuntimeError(raw_sentinel)
            return await super().invoke(action, node=node)

    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
    )
    context, tools, _, events, _ = context_for(
        scripts,
        (query_result(("gmv",), (("not-a-number",),)),),
    )
    context = replace_context(context, tools=ExplodingRepairTools(tools.registry))

    result = await run_agent(run_id="repair-port-failure", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.REPAIR_FAILED
    assert result.repair_history[-1].outcome == "failed"
    assert result.repair_history[-1].repaired_observation_id is None
    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.governance.action_loops == 1
    assert raw_sentinel not in json.dumps(
        {
            "answer": result.final_answer.model_dump(mode="json"),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )


@pytest.mark.asyncio
async def test_injected_tool_metadata_mismatch_is_replaced_with_safe_failure() -> None:
    malicious_purpose = "api_key_prompt_payload"

    class MismatchedTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            del action, node
            observation = Observation(
                observation_id="malicious-observation",
                tool_name=ActionType.EXECUTE_SQL,
                purpose=malicious_purpose,
                ok=False,
                safe_error="database_error",
                hypothesis_id="metric_value",
                contract_id="metric_value_contract",
            )
            trace = ToolCallTrace(
                tool_name=ActionType.EXECUTE_SQL,
                purpose=malicious_purpose,
                safe_arguments=(
                    ("contract_id", "metric_value_contract"),
                    ("hypothesis_id", "metric_value"),
                ),
                safe_error="database_error",
            )
            return ToolInvocation(observation=observation, trace=trace)

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=MismatchedTools(tools.registry))

    result = await run_agent(run_id="mismatched-tool", query=QUERY, context=context)

    assert result.observations[-1].purpose == "metric_value_contract"
    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.safe_trace.tool_calls[-1].purpose == "metric_value_contract"
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert malicious_purpose not in json.dumps(
        {
            "answer": result.final_answer.model_dump(mode="json"),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )


@pytest.mark.asyncio
async def test_tool_started_event_failure_clears_and_consumes_pending_loop() -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, _, _, _, _ = context_for(scripts, ())
    context = replace_context(context, events=FailOneEvent("tool.started"))

    result = await run_agent(run_id="tool-event-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert not any(item.tool_name is ActionType.EXECUTE_SQL for item in result.observations)


@pytest.mark.asyncio
async def test_preinvoke_tool_budget_rejection_clears_pending_without_consuming_loop() -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, _, _, _, _ = context_for(scripts, (), budget=ledger(max_tool_calls=2))
    graph = build_agent_graph()

    state = await graph.ainvoke(
        new_agent_state(run_id="preinvoke-budget", query=QUERY),
        context=context,
    )

    assert state["stop_reason"] is StopReason.TOOL_CALL_LIMIT
    assert state["action_loop_pending"] is False
    assert state["governance"].execute_calls == 0
    assert state["governance"].action_loops == 0
    assert not any(item.tool_name is ActionType.EXECUTE_SQL for item in state["observations"])


@pytest.mark.asyncio
async def test_event_sink_exception_returns_internal_terminal_without_raw_error() -> None:
    context, _, _, _, _ = context_for(scripts_for(QUERY), ())
    context = replace_context(context, events=FailingEvents())

    result = await run_agent(run_id="event-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR
    assert "raw event sink sentinel" not in result.final_answer.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["append_node", "append_model", "append_tool", "snapshot"])
async def test_trace_dependency_failure_returns_internal_terminal(failure: str) -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, _, model, events, _ = context_for(scripts, (query_result(),))
    recorder = FailingTraceRecorder(failure)
    context = replace_context(context, model=model, recorder=recorder)

    result = await run_agent(run_id=f"trace-{failure}", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR
    assert (
        "raw"
        not in json.dumps(
            {
                "answer": result.final_answer.model_dump(mode="json"),
                "trace": result.safe_trace.model_dump(mode="json"),
                "events": events.items,
            }
        ).lower()
    )
    if failure == "append_tool":
        assert result.governance.execute_calls == result.governance.action_loops == 1


@pytest.mark.asyncio
async def test_invoke_tool_node_trace_failure_atomically_consumes_pending_loop() -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, _, model, _, _ = context_for(scripts, (query_result(),))
    recorder = FailingTraceRecorder("append_node_invoke_tool")
    context = replace_context(context, model=model, recorder=recorder)

    result = await run_agent(run_id="tool-node-trace-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.governance.execute_calls == result.governance.action_loops == 1


@pytest.mark.asyncio
async def test_malicious_plan_identifier_is_rejected_before_plan_event_or_state() -> None:
    malicious = simple_plan()
    malicious["plan_id"] = "api_key"
    context, _, model, events, _ = context_for(
        scripts_for(QUERY, plan=malicious),
        (),
    )

    result = await run_agent(run_id="malicious-plan", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.EXECUTION_FAILED
    assert result.final_answer.stop_reason is StopReason.PLAN_INVALID
    assert [call.purpose for call in model.calls] == ["behavior", "plan"]
    assert not any(item[1] == "plan.created" for item in events.items)
    assert "api_key" not in json.dumps(events.items)


@pytest.mark.asyncio
async def test_malicious_action_purpose_is_rejected_before_tool_call() -> None:
    action = execute_action()
    action["purpose"] = "secret_token"
    context, tools, _, events, backend = context_for(
        scripts_for(QUERY, plan=simple_plan(), actions=(action,)),
        (),
    )

    result = await run_agent(run_id="malicious-action", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.PLAN_INVALID
    assert tools.calls == [ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP]
    assert backend.calls == 0
    assert "secret_token" not in json.dumps(events.items)


def bound_synthesis(
    request: StructuredModelRequest,
    *,
    answer: str = "模型答案标记。",
    evidence_ids: tuple[str, ...] | None = None,
    completed_dimensions: tuple[str, ...] = ("metric_value",),
    missing_dimensions: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    result_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": "completed",
        "stop_reason": "answer_complete",
        "answer": answer,
        "evidence_ids": (
            list(evidence_ids)
            if evidence_ids is not None
            else list(evidence_ids_from_request(request))
        ),
        "completed_dimensions": list(completed_dimensions),
        "missing_dimensions": list(missing_dimensions),
        "limitations": list(limitations),
        "result_summary": result_summary,
    }


def projected_summary_from_request(request: StructuredModelRequest) -> dict[str, object]:
    raw_evidence = request.user_payload["evidence"]
    assert isinstance(raw_evidence, tuple)
    projected: list[dict[str, object]] = []
    for item in raw_evidence:
        assert isinstance(item, Mapping)
        raw_dimensions = item["dimensions"]
        assert isinstance(raw_dimensions, tuple)
        projected.append(
            {
                "evidence_id": item["evidence_id"],
                "claim_key": item["claim_key"],
                "numeric_value": item["numeric_value"],
                "unit": item["unit"],
                "dimensions": tuple(
                    {"name": pair[0], "value": pair[1]}
                    for pair in raw_dimensions
                    if isinstance(pair, tuple) and len(pair) == 2
                ),
            }
        )
    return {"evidence": tuple(projected)}


@pytest.mark.asyncio
async def test_exactly_bound_synthesis_is_accepted_and_summary_is_governed() -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))

    def factory(request: StructuredModelRequest) -> dict[str, object]:
        return bound_synthesis(
            request,
            result_summary=projected_summary_from_request(request),
        )

    context, _, _, _, _ = context_for(scripts, (query_result(),))
    model = DynamicSynthesisModel(scripts, factory)
    context = replace_context(context, model=model)

    result = await run_agent(run_id="bound-synthesis", query=QUERY, context=context)

    assert result.final_answer.answer == "已基于受治理且验证通过的证据完成分析。"
    assert result.final_answer.evidence_ids == tuple(item.evidence_id for item in result.evidence)
    assert result.final_answer.completed_dimensions == ("metric_value",)
    assert result.final_answer.missing_dimensions == ()
    assert result.final_answer.result_summary is not None
    summary_items = result.final_answer.result_summary["evidence"]
    assert isinstance(summary_items, tuple)
    first_summary = summary_items[0]
    assert isinstance(first_summary, Mapping)
    assert first_summary["numeric_value"] == "125.00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    [
        lambda request: bound_synthesis(request, evidence_ids=()),
        lambda request: bound_synthesis(request, completed_dimensions=()),
        lambda request: bound_synthesis(request, answer="结果是 999999999。"),
        lambda request: bound_synthesis(request, limitations=("API_KEY=private",)),
        lambda request: bound_synthesis(
            request,
            result_summary={"evidence": ({"evidence_id": "wrong"},)},
        ),
        lambda request: bound_synthesis(
            request,
            result_summary={"note": "password private"},
        ),
    ],
    ids=[
        "missing-ids",
        "wrong-dimensions",
        "wrong-number",
        "api-key",
        "wrong-summary",
        "sensitive-summary",
    ],
)
async def test_unbound_or_sensitive_synthesis_falls_back_deterministically(
    factory: SynthesisFactory,
) -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, _, _, _, _ = context_for(scripts, (query_result(),))
    model = DynamicSynthesisModel(scripts, factory)
    context = replace_context(context, model=model)

    result = await run_agent(run_id="invalid-synthesis", query=QUERY, context=context)

    assert result.final_answer.answer != "模型答案标记。"
    assert "999999999" not in result.final_answer.model_dump_json()
    assert "api_key" not in result.final_answer.model_dump_json().lower()
    assert result.final_answer.evidence_ids == tuple(item.evidence_id for item in result.evidence)
    assert result.final_answer.completed_dimensions == ("metric_value",)
    assert result.final_answer.result_summary is not None


@pytest.mark.asyncio
async def test_profile_event_and_trace_never_include_filter_value() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(profile_action(), execute_action()),
        synthesis_output=synthesis(),
    )
    context, _, _, events, _ = context_for(scripts, (query_result(),))

    result = await run_agent(run_id="profile-events", query=QUERY, context=context)

    profile_started = next(
        item
        for item in events.items
        if item[1] == "tool.started" and item[2]["tool_name"] == "profile"
    )
    profile_completed = next(
        item
        for item in events.items
        if item[1] == "tool.completed" and item[2]["tool_name"] == "profile"
    )
    assert profile_started[2]["purpose"] == "profile_context"
    assert profile_completed[2]["purpose"] == "profile_context"
    rendered = json.dumps(
        {
            "events": events.items,
            "trace": result.safe_trace.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )
    assert "private_filter_value_sentinel" not in rendered


@pytest.mark.asyncio
async def test_injected_raw_safe_error_is_replaced_by_allowlisted_internal_error() -> None:
    raw_safe_error = "raw_api_key_safe_error_sentinel"

    class RawDiagnosticTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            typed = cast(AgentAction, action)
            self.calls.append(typed.action_type)
            observation = Observation(
                observation_id="injected-observation",
                tool_name=typed.action_type,
                purpose="metric_value_contract",
                ok=False,
                safe_error=raw_safe_error,
                hypothesis_id="metric_value",
                contract_id="metric_value_contract",
            )
            trace = ToolCallTrace(
                tool_name=typed.action_type,
                purpose="metric_value_contract",
                safe_arguments=(
                    ("contract_id", "metric_value_contract"),
                    ("hypothesis_id", "metric_value"),
                ),
                safe_error=raw_safe_error,
            )
            return ToolInvocation(observation=observation, trace=trace)

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=RawDiagnosticTools(tools.registry))

    result = await run_agent(run_id="raw-safe-error", query=QUERY, context=context)

    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.safe_trace.tool_calls[-1].safe_error == "internal_tool_error"
    rendered = json.dumps(
        {
            "observation": result.observations[-1].model_dump(mode="json"),
            "first_candidate": (
                result.first_candidate.model_dump(mode="json")
                if result.first_candidate is not None
                else None
            ),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )
    assert raw_safe_error not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_result",
    [
        pytest.param(None, id="none"),
        pytest.param(InvalidToolReturn(), id="plain-object"),
        pytest.param(
            ToolInvocation.model_construct(
                observation=InvalidToolReturn(),
                trace=InvalidToolReturn(),
            ),
            id="malformed-tool-invocation",
        ),
    ],
)
async def test_invalid_tool_invocation_result_is_one_safe_internal_failure(
    bad_result: object,
) -> None:
    class InvalidResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            del node
            self.calls.append(cast(AgentAction, action).action_type)
            return bad_result

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=InvalidResultTools(tools.registry))

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="invalid-tool-result", query=QUERY),
            context=context,
        ),
    )

    assert state["final_answer"] is not None
    assert state["final_answer"].status is FinalStatus.INTERNAL_ERROR
    assert state["final_answer"].stop_reason is StopReason.INTERNAL_ERROR
    assert state["governance"].execute_calls == 1
    assert state["governance"].action_loops == 1
    assert state["action_loop_pending"] is False
    execute_observations = tuple(
        item for item in state["observations"] if item.tool_name is ActionType.EXECUTE_SQL
    )
    execute_traces = tuple(
        item for item in state["tool_call_traces"] if item.tool_name is ActionType.EXECUTE_SQL
    )
    assert len(execute_observations) == 1
    assert execute_observations[0].safe_error == "internal_tool_error"
    assert len(execute_traces) == 1
    assert execute_traces[0].safe_error == "internal_tool_error"
    assert [item[1] for item in events.items].count("tool.failed") == 1
    rendered = json.dumps(
        {
            "observation": execute_observations[0].model_dump(mode="json"),
            "trace": execute_traces[0].model_dump(mode="json"),
            "events": events.items,
        }
    )
    assert RAW_INVALID_TOOL_RESULT_SENTINEL not in rendered


@pytest.mark.asyncio
async def test_invalid_repair_tool_result_is_one_safe_failed_repair() -> None:
    class InvalidRepairResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            if node == "repair":
                self.calls.append(cast(AgentAction, action).action_type)
                return InvalidToolReturn()
            return await super().invoke(action, node=node)

    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="select cast(125 as numeric) as value"),),
        repairs=(execute_action(),),
    )
    context, tools, _, events, _ = context_for(
        scripts,
        (query_result(("value",), (("125",),)),),
    )
    context = replace_context(context, tools=InvalidRepairResultTools(tools.registry))

    result = await run_agent(run_id="invalid-repair-tool-result", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.EXECUTION_FAILED
    assert result.final_answer.stop_reason is StopReason.REPAIR_FAILED
    assert result.governance.execute_calls == 2
    assert result.governance.action_loops == 1
    assert result.governance.repair_count == 1
    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.safe_trace.tool_calls[-1].safe_error == "internal_tool_error"
    execute_observations = tuple(
        item for item in result.observations if item.tool_name is ActionType.EXECUTE_SQL
    )
    execute_traces = tuple(
        item for item in result.safe_trace.tool_calls if item.tool_name is ActionType.EXECUTE_SQL
    )
    assert len(execute_observations) == len(execute_traces) == 2
    assert [item[1] for item in events.items].count("tool.failed") == 1
    assert RAW_INVALID_TOOL_RESULT_SENTINEL not in json.dumps(
        {
            "observations": [item.model_dump(mode="json") for item in execute_observations],
            "traces": [item.model_dump(mode="json") for item in execute_traces],
            "events": events.items,
        }
    )


@pytest.mark.asyncio
async def test_injected_profile_trace_must_exactly_match_governed_action() -> None:
    forged_table = "forged_profile_table_sentinel"

    class ForgedProfileTraceTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            typed = cast(AgentAction, action)
            if typed.action_type is not ActionType.PROFILE:
                return invocation
            forged = invocation.trace.model_copy(
                update={
                    "safe_arguments": (
                        ("column_name", "region"),
                        ("filter_columns", ("status",)),
                        ("has_time_window", False),
                        ("limit", 5),
                        ("operation", "top_values"),
                        ("table_name", forged_table),
                    )
                }
            )
            return invocation.model_copy(update={"trace": forged})

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(profile_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=ForgedProfileTraceTools(tools.registry))

    result = await run_agent(run_id="forged-profile-trace", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.safe_trace.tool_calls[-1].safe_arguments == (
        ("column_name", "region"),
        ("filter_columns", ("status",)),
        ("has_time_window", False),
        ("limit", 5),
        ("operation", "top_values"),
        ("table_name", "orders"),
    )
    assert forged_table not in json.dumps(
        {"trace": result.safe_trace.model_dump(mode="json"), "events": events.items}
    )


@pytest.mark.asyncio
async def test_valid_profile_trace_uses_exact_deterministic_safe_arguments() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(profile_action(), execute_action()),
        synthesis_output=synthesis(),
    )
    context, _, _, _, _ = context_for(scripts, (query_result(),))

    result = await run_agent(run_id="valid-profile-trace", query=QUERY, context=context)

    profile_trace = next(
        item for item in result.safe_trace.tool_calls if item.tool_name is ActionType.PROFILE
    )
    assert profile_trace.safe_arguments == (
        ("column_name", "region"),
        ("filter_columns", ("status",)),
        ("has_time_window", False),
        ("limit", 5),
        ("operation", "top_values"),
        ("table_name", "orders"),
    )


@pytest.mark.asyncio
async def test_profile_invalid_request_may_retain_exact_governed_safe_arguments() -> None:
    class InvalidProfileTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            typed = cast(AgentAction, action)
            self.calls.append(typed.action_type)
            safe_arguments: tuple[tuple[str, JsonValue], ...] = (
                ("column_name", "region"),
                ("filter_columns", ("status",)),
                ("has_time_window", False),
                ("limit", 5),
                ("operation", "top_values"),
                ("table_name", "orders"),
            )
            return ToolInvocation(
                observation=Observation(
                    observation_id="profile-invalid-request",
                    tool_name=ActionType.PROFILE,
                    purpose="profile_context",
                    ok=False,
                    safe_error="invalid_request",
                    hypothesis_id="metric_value",
                ),
                trace=ToolCallTrace(
                    tool_name=ActionType.PROFILE,
                    purpose="profile_context",
                    safe_arguments=safe_arguments,
                    safe_error="invalid_request",
                ),
            )

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(profile_action(),))
    context, tools, _, _, _ = context_for(scripts, ())
    context = replace_context(context, tools=InvalidProfileTools(tools.registry))

    result = await run_agent(run_id="profile-invalid-exact", query=QUERY, context=context)

    assert result.observations[-1].safe_error == "invalid_request"
    assert result.safe_trace.tool_calls[-1].safe_error == "invalid_request"
    assert result.safe_trace.tool_calls[-1].safe_arguments[0] == (
        "column_name",
        "region",
    )


@pytest.mark.asyncio
async def test_profile_observation_and_trace_result_metadata_must_match() -> None:
    forged_column = "forged_trace_column_sentinel"

    class ForgedResultTraceTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            if cast(AgentAction, action).action_type is ActionType.PROFILE:
                invocation = invocation.model_copy(
                    update={
                        "observation": invocation.observation.model_copy(
                            update={"columns": (forged_column,)}
                        ),
                        "trace": invocation.trace.model_copy(update={"columns": (forged_column,)}),
                    }
                )
            return invocation

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(profile_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=ForgedResultTraceTools(tools.registry))

    result = await run_agent(run_id="profile-result-mismatch", query=QUERY, context=context)

    assert result.observations[-1].safe_error == "internal_tool_error"
    assert forged_column not in json.dumps(
        {"trace": result.safe_trace.model_dump(mode="json"), "events": events.items}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("table_name", "unknown_table"),
        ("column_name", "unknown_column"),
        ("filter_column", "secret_token"),
    ],
)
async def test_profile_action_identifiers_are_bound_to_retrieved_schema(
    field: str,
    value: str,
) -> None:
    action = profile_action()
    arguments = cast(dict[str, object], action["arguments"])
    if field == "filter_column":
        arguments["filters"] = [{"column_name": value, "value": "private"}]
    else:
        arguments[field] = value
    context, tools, _, events, backend = context_for(
        scripts_for(QUERY, plan=simple_plan(), actions=(action,)),
        (),
    )

    result = await run_agent(run_id=f"profile-schema-{field}", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.PLAN_INVALID
    assert ActionType.PROFILE not in tools.calls
    assert backend.calls == 0
    assert value not in json.dumps(events.items)


@pytest.mark.asyncio
async def test_langgraph_node_cancellation_propagates_as_asyncio_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancelledGraph:
        async def ainvoke(self, *_args: object, **_kwargs: object) -> object:
            raise NodeCancelledError("intake")

    monkeypatch.setattr(agent_graph_module, "build_agent_graph", CancelledGraph)
    context, _, _, _, _ = context_for({}, ())

    with pytest.raises(asyncio.CancelledError):
        await run_agent(run_id="node-cancelled", query=QUERY, context=context)


@pytest.mark.asyncio
async def test_active_task_cancellation_wins_over_cleanup_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingGraph:
        async def ainvoke(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("cleanup replaced cancellation sentinel")

    class CancellingTask:
        def cancelling(self) -> int:
            return 1

    monkeypatch.setattr(agent_graph_module, "build_agent_graph", ExplodingGraph)
    monkeypatch.setattr(asyncio, "current_task", lambda: CancellingTask())
    context, _, _, _, _ = context_for({}, ())

    with pytest.raises(asyncio.CancelledError):
        await run_agent(run_id="cleanup-cancelled", query=QUERY, context=context)


@pytest.mark.asyncio
async def test_internal_fallback_preserves_live_trace_appended_before_recorder_failure() -> None:
    context, _, model, _, _ = context_for({}, ())
    recorder = AppendThenFailTraceRecorder()
    context = replace_context(context, model=model, recorder=recorder)

    result = await run_agent(run_id="live-fallback", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert tuple(item.node for item in result.safe_trace.nodes) == ("intake", "finalize")
    assert result.governance == context.budget.snapshot


@pytest.mark.asyncio
async def test_repaired_observation_is_committed_only_after_validation_and_completion_events() -> (
    None
):
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
    )
    context, _, _, _, _ = context_for(
        scripts,
        (
            query_result(("gmv",), (("not-a-number",),)),
            query_result(),
        ),
    )
    events = FailNthEvent("observation.validated", 2)
    context = replace_context(context, events=events)

    result = await run_agent(run_id="repair-validation-event-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert not any(item.kind == "result_contract" for item in result.repair_history)
    assert not any(item[1] == "repair.completed" for item in events.items)


@pytest.mark.asyncio
async def test_repair_completion_event_failure_does_not_commit_success_record() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
    )
    context, _, _, _, _ = context_for(
        scripts,
        (
            query_result(("gmv",), (("not-a-number",),)),
            query_result(),
        ),
    )
    events = FailOneEvent("repair.completed")
    context = replace_context(context, events=events)

    result = await run_agent(run_id="repair-completion-event-failure", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert len(result.observation_validations) == 1
    assert not result.observation_validations[-1].valid
    assert not any(item.kind == "result_contract" for item in result.repair_history)


@pytest.mark.asyncio
async def test_failed_repair_completion_event_is_not_retried_or_committed() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
    )
    context, _, _, _, _ = context_for(
        scripts,
        (query_result(("gmv",), (("not-a-number",),)),),
    )

    class ExplodingRepairTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            if node == "repair":
                raise RuntimeError("raw repair tool sentinel")
            return await super().invoke(action, node=node)

    events = FailFirstEvent("repair.completed")
    context = replace_context(
        context,
        tools=ExplodingRepairTools(cast(RecordingTools, context.tools).registry),
        events=events,
    )

    result = await run_agent(run_id="failed-repair-completion-event", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert events.attempts == 1
    assert not any(item[1] == "repair.completed" for item in events.items)
    assert not any(item.kind == "result_contract" for item in result.repair_history)


@pytest.mark.asyncio
async def test_failed_observation_validation_event_consumes_action_loop_exactly_once() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="drop table orders"),),
    )
    context, _, _, _, _ = context_for(scripts, ())
    context = replace_context(context, events=FailOneEvent("observation.validated"))

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="failed-validation-event", query=QUERY),
            context=context,
        ),
    )

    assert state["final_answer"] is not None
    assert state["final_answer"].status is FinalStatus.INTERNAL_ERROR
    assert state["governance"].execute_calls == 1
    assert state["governance"].action_loops == 1
    assert state["action_loop_pending"] is False


@pytest.mark.asyncio
async def test_repair_budget_rejection_warns_without_orphan_completion_event() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        repairs=(execute_action(),),
    )
    context, _, _, events, _ = context_for(
        scripts,
        (query_result(("gmv",), (("not-a-number",),)),),
        budget=ledger(max_repairs=0),
    )

    result = await run_agent(run_id="repair-budget-rejected", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.REPAIR_FAILED
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert result.governance.repair_count == 0
    assert [item[1] for item in events.items].count("budget.warning") == 1
    assert not any(item[1] in {"repair.started", "repair.completed"} for item in events.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["unknown_field", "api_key", "secret_token"])
async def test_behavior_missing_fields_are_restricted_to_governed_business_fields(
    missing_field: str,
) -> None:
    scripts = scripts_for(
        QUERY,
        behavior={
            "action": "clarify",
            "reason_code": "missing_time_window",
            "missing_fields": [missing_field],
            "user_message": "请补充信息。",
        },
    )
    context, tools, _, events, _ = context_for(scripts, ())

    result = await run_agent(run_id="unsafe-missing-field", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.STRUCTURED_OUTPUT_INVALID
    assert tools.calls == []
    assert not any(item[1] == "behavior.decided" for item in events.items)
    assert missing_field not in json.dumps(events.items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_answer",
    [
        "结果是九亿, model_sentinel。",
        "结果是 999999999, model_sentinel。",
        "结果是 1,250, model_sentinel。",
        "变化率是 25%, model_sentinel。",
        "row update filter are ordinary English words; model_sentinel.",
        "API_KEY=private; model_sentinel.",
    ],
)
async def test_synthesis_model_free_text_never_crosses_final_answer_boundary(
    model_answer: str,
) -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))

    def factory(request: StructuredModelRequest) -> dict[str, object]:
        return bound_synthesis(
            request,
            answer=model_answer,
            limitations=(model_answer,),
            result_summary=projected_summary_from_request(request),
        )

    context, _, _, _, _ = context_for(scripts, (query_result(),))
    context = replace_context(context, model=DynamicSynthesisModel(scripts, factory))

    result = await run_agent(run_id="deterministic-synthesis", query=QUERY, context=context)

    assert result.final_answer.answer == "已基于受治理且验证通过的证据完成分析。"
    assert result.final_answer.limitations == ()
    assert "model_sentinel" not in result.final_answer.model_dump_json()
    assert result.final_answer.evidence_ids == tuple(item.evidence_id for item in result.evidence)
    summary = result.final_answer.result_summary
    assert summary is not None
    summary_evidence = summary["evidence"]
    assert isinstance(summary_evidence, tuple)
    first = summary_evidence[0]
    assert isinstance(first, Mapping)
    assert first["numeric_value"] == "125.00"
    assert first["unit"] == "cny"


@pytest.mark.asyncio
async def test_partial_synthesis_requires_exact_evidence_partial_reason() -> None:
    budget = SoftAfterExecuteBudget(ledger())
    scripts = scripts_for(
        ATTRIBUTION_QUERY,
        plan=attribution_plan(),
        actions=(
            execute_action(
                "gmv_comparison",
                "confirm_decline",
                sql=(
                    "select 80::numeric as current_gmv, 100::numeric as previous_gmv, "
                    "-0.2::numeric as change_rate"
                ),
            ),
        ),
    )
    context, _, _, _, _ = context_for(
        scripts,
        (
            query_result(
                ("current_gmv", "previous_gmv", "change_rate"),
                (("80", "100", "-0.2"),),
            ),
        ),
        budget=budget,
    )
    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="partial-state", query=ATTRIBUTION_QUERY),
            context=context,
        ),
    )

    def forged_partial(request: StructuredModelRequest) -> dict[str, object]:
        return {
            "status": "partial",
            "stop_reason": "result_truncated",
            "answer": "model_partial_sentinel",
            "evidence_ids": list(evidence_ids_from_request(request)),
            "completed_dimensions": ["confirm_decline"],
            "missing_dimensions": [
                "region_contribution",
                "segment_contribution",
                "sku_contribution",
            ],
        }

    synthesis_model = DynamicSynthesisModel(scripts, forged_partial)
    synthesis_context = replace_context(context, model=synthesis_model)
    state["final_answer"] = None
    state["stop_reason"] = StopReason.EVIDENCE_PARTIAL

    delta = await synthesize_node(state, Runtime(context=synthesis_context))

    answer = cast(FinalAnswer, delta["final_answer"])
    assert answer.status is FinalStatus.PARTIAL
    assert answer.stop_reason is StopReason.EVIDENCE_PARTIAL
    assert delta["stop_reason"] is StopReason.EVIDENCE_PARTIAL


@pytest.mark.asyncio
async def test_judge_evidence_trace_failure_cannot_finalize_completed() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, model, _, _ = context_for(scripts, (query_result(),))
    context = replace_context(
        context,
        model=model,
        recorder=FailingTraceRecorder("append_node_judge_evidence"),
    )

    result = await run_agent(run_id="judge-trace-terminal", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR
    assert result.governance.execute_calls == result.governance.action_loops == 1


@pytest.mark.asyncio
async def test_synthesize_trace_failure_cannot_finalize_completed() -> None:
    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, _, model, _, _ = context_for(scripts, (query_result(),))
    context = replace_context(
        context,
        model=model,
        recorder=FailingTraceRecorder("append_node_synthesize"),
    )

    result = await run_agent(run_id="synthesize-trace-terminal", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.final_answer.stop_reason is StopReason.INTERNAL_ERROR
    assert result.governance.execute_calls == result.governance.action_loops == 1


@pytest.mark.asyncio
async def test_execute_success_columns_must_match_current_answer_contract() -> None:
    malicious_column = "api_key"

    class ForgedExecuteColumnsTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            if cast(AgentAction, action).action_type is ActionType.EXECUTE_SQL:
                invocation = invocation.model_copy(
                    update={
                        "observation": invocation.observation.model_copy(
                            update={"columns": (malicious_column,)}
                        ),
                        "trace": invocation.trace.model_copy(
                            update={"columns": (malicious_column,)}
                        ),
                    }
                )
            return invocation

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, (query_result(),))
    context = replace_context(context, tools=ForgedExecuteColumnsTools(tools.registry))

    result = await run_agent(run_id="forged-execute-columns", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.safe_trace.tool_calls[-1].safe_error == "internal_tool_error"
    rendered = json.dumps(
        {
            "observations": [dict(item.safe_summary) for item in result.observations],
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )
    assert malicious_column not in rendered


@pytest.mark.asyncio
async def test_profile_invalid_request_requires_exact_safe_arguments() -> None:
    class EmptyProfileArgumentsTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            typed = cast(AgentAction, action)
            self.calls.append(typed.action_type)
            return ToolInvocation(
                observation=Observation(
                    observation_id="profile-empty-invalid-request",
                    tool_name=ActionType.PROFILE,
                    purpose="profile_context",
                    ok=False,
                    safe_error="invalid_request",
                    hypothesis_id="metric_value",
                ),
                trace=ToolCallTrace(
                    tool_name=ActionType.PROFILE,
                    purpose="profile_context",
                    safe_arguments=(),
                    safe_error="invalid_request",
                ),
            )

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(profile_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=EmptyProfileArgumentsTools(tools.registry))

    result = await run_agent(run_id="profile-empty-safe-args", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.observations[-1].safe_error == "internal_tool_error"
    assert result.safe_trace.tool_calls[-1].safe_arguments == (
        ("column_name", "region"),
        ("filter_columns", ("status",)),
        ("has_time_window", False),
        ("limit", 5),
        ("operation", "top_values"),
        ("table_name", "orders"),
    )
    assert not any(
        item[1] == "tool.failed" and item[2]["safe_error"] == "invalid_request"
        for item in events.items
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "column_name", "time_column"),
    [
        ("numeric_summary", "region", None),
        ("time_range", "region", None),
        ("top_values", "region", "region"),
    ],
)
async def test_profile_action_must_match_schema_column_types(
    operation: str,
    column_name: str,
    time_column: str | None,
) -> None:
    action = profile_action()
    arguments = cast(dict[str, object], action["arguments"])
    arguments["operation"] = operation
    arguments["column_name"] = column_name
    if time_column is not None:
        arguments.update(
            {
                "time_column": time_column,
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-07-01T00:00:00Z",
            }
        )
    context, tools, _, _, _ = context_for(
        scripts_for(QUERY, plan=simple_plan(), actions=(action,)),
        (),
    )

    result = await run_agent(run_id="profile-schema-type", query=QUERY, context=context)

    assert result.final_answer.stop_reason is StopReason.PLAN_INVALID
    assert ActionType.PROFILE not in tools.calls


@pytest.mark.asyncio
async def test_valid_execute_validation_trace_failure_consumes_loop_once() -> None:
    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, _, model, _, _ = context_for(scripts, (query_result(),))
    context = replace_context(
        context,
        model=model,
        recorder=FailingTraceRecorder("append_node_validate_observation"),
    )

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="validation-trace-failure", query=QUERY),
            context=context,
        ),
    )

    assert state["final_answer"] is not None
    assert state["final_answer"].status is FinalStatus.INTERNAL_ERROR
    assert state["governance"].execute_calls == 1
    assert state["governance"].action_loops == 1
    assert state["action_loop_pending"] is False


@pytest.mark.asyncio
async def test_node_self_cancellation_wins_over_tool_cleanup_runtime_error() -> None:
    class CancellingTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            del action, node
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise RuntimeError("cleanup replaced node cancellation sentinel") from None
            raise AssertionError("cancellation was not delivered")

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, _, _ = context_for(scripts, ())
    context = replace_context(context, tools=CancellingTools(tools.registry))

    with pytest.raises(asyncio.CancelledError):
        await run_agent(run_id="node-self-cancelled", query=QUERY, context=context)


def malformed_tool_invocation(field: str) -> ToolInvocation:
    observation_values: dict[str, object] = {
        "observation_id": "malformed-observation",
        "tool_name": ActionType.EXECUTE_SQL,
        "purpose": "metric_value_contract",
        "ok": True,
        "safe_error": None,
        "hypothesis_id": "metric_value",
        "contract_id": "metric_value_contract",
        "query_id": QUERY_ID,
        "columns": ("gmv",),
        "row_count": 1,
        "possibly_truncated": False,
        "payload": MappingProxyType({"rows": (("125.00",),)}),
    }
    trace_values: dict[str, object] = {
        "tool_name": ActionType.EXECUTE_SQL,
        "purpose": "metric_value_contract",
        "safe_arguments": (
            ("contract_id", "metric_value_contract"),
            ("hypothesis_id", "metric_value"),
        ),
        "query_id": QUERY_ID,
        "columns": ("gmv",),
        "row_count": 1,
        "possibly_truncated": False,
        "safe_error": None,
    }
    if field == "query_id":
        observation_values["query_id"] = b"a" * 64
        trace_values["query_id"] = b"a" * 64
    elif field == "columns":
        observation_values["columns"] = ["gmv"]
        trace_values["columns"] = ["gmv"]
    elif field == "rows":
        observation_values["payload"] = MappingProxyType({"rows": [["125.00"]]})
    elif field == "row_count":
        observation_values["row_count"] = True
        trace_values["row_count"] = True
    elif field == "malformed_column":
        malformed = MalformedField()
        observation_values["columns"] = (malformed,)
        trace_values["columns"] = (malformed,)
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(field)
    return ToolInvocation.model_construct(
        observation=Observation.model_construct(**observation_values),  # type: ignore[arg-type]
        trace=ToolCallTrace.model_construct(**trace_values),  # type: ignore[arg-type]
    )


def assert_one_safe_execute_failure(
    state: AgentState,
    events: RecordingEvents,
    *,
    execute_calls: int,
) -> None:
    assert state["final_answer"] is not None
    assert state["final_answer"].status is FinalStatus.INTERNAL_ERROR
    assert state["final_answer"].stop_reason is StopReason.INTERNAL_ERROR
    assert state["governance"].execute_calls == execute_calls
    assert state["governance"].action_loops == 1
    assert state["action_loop_pending"] is False
    execute_observations = tuple(
        item for item in state["observations"] if item.tool_name is ActionType.EXECUTE_SQL
    )
    execute_traces = tuple(
        item for item in state["tool_call_traces"] if item.tool_name is ActionType.EXECUTE_SQL
    )
    assert len(execute_observations) == len(execute_traces) == execute_calls
    assert execute_observations[-1].safe_error == "internal_tool_error"
    assert execute_traces[-1].safe_error == "internal_tool_error"
    assert [item[1] for item in events.items].count("tool.failed") == 1


@pytest.mark.asyncio
async def test_tool_result_class_getter_proxy_is_safely_rejected_without_field_access() -> None:
    proxy = ExplodingClassProxy()
    type(proxy).observation_reads = 0
    type(proxy).trace_reads = 0

    class ProxyResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            del node
            self.calls.append(cast(AgentAction, action).action_type)
            return proxy

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=ProxyResultTools(tools.registry))

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="class-proxy-tool-result", query=QUERY),
            context=context,
        ),
    )

    assert_one_safe_execute_failure(state, events, execute_calls=1)
    assert type(proxy).observation_reads == type(proxy).trace_reads == 0
    rendered = json.dumps(
        {
            "observations": [item.model_dump(mode="json") for item in state["observations"]],
            "traces": [item.model_dump(mode="json") for item in state["tool_call_traces"]],
            "events": events.items,
        }
    )
    assert CLASS_GETTER_SENTINEL not in rendered


@pytest.mark.asyncio
async def test_repair_tool_result_class_getter_proxy_is_safely_rejected_and_audited() -> None:
    proxy = ExplodingClassProxy()
    type(proxy).observation_reads = 0
    type(proxy).trace_reads = 0

    class ProxyRepairResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            if node == "repair":
                self.calls.append(cast(AgentAction, action).action_type)
                return proxy
            return await super().invoke(action, node=node)

    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(sql="select cast(125 as numeric) as value"),),
        repairs=(execute_action(),),
    )
    context, tools, _, events, _ = context_for(
        scripts,
        (query_result(("value",), (("125",),)),),
    )
    context = replace_context(context, tools=ProxyRepairResultTools(tools.registry))

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="class-proxy-repair-result", query=QUERY),
            context=context,
        ),
    )

    assert state["final_answer"] is not None
    assert state["final_answer"].status is FinalStatus.EXECUTION_FAILED
    assert state["final_answer"].stop_reason is StopReason.REPAIR_FAILED
    assert state["governance"].execute_calls == 2
    assert state["governance"].action_loops == 1
    assert state["governance"].repair_count == 1
    assert state["action_loop_pending"] is False
    execute_observations = tuple(
        item for item in state["observations"] if item.tool_name is ActionType.EXECUTE_SQL
    )
    execute_traces = tuple(
        item for item in state["tool_call_traces"] if item.tool_name is ActionType.EXECUTE_SQL
    )
    assert len(execute_observations) == len(execute_traces) == 2
    assert execute_observations[-1].safe_error == "internal_tool_error"
    assert execute_traces[-1].safe_error == "internal_tool_error"
    assert [item[1] for item in events.items].count("tool.failed") == 1
    assert type(proxy).observation_reads == type(proxy).trace_reads == 0
    assert CLASS_GETTER_SENTINEL not in json.dumps(
        {
            "observations": [item.model_dump(mode="json") for item in execute_observations],
            "traces": [item.model_dump(mode="json") for item in execute_traces],
            "events": events.items,
        }
    )


@pytest.mark.asyncio
async def test_tool_invocation_subclass_is_rejected_without_nested_field_access() -> None:
    class SubclassResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            subclass = CountingToolInvocation(
                observation=invocation.observation,
                trace=invocation.trace,
            )
            CountingToolInvocation.observation_reads = 0
            CountingToolInvocation.trace_reads = 0
            return subclass

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, (query_result(),))
    context = replace_context(context, tools=SubclassResultTools(tools.registry))

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id="subclass-tool-result", query=QUERY),
            context=context,
        ),
    )

    assert_one_safe_execute_failure(state, events, execute_calls=1)
    assert CountingToolInvocation.observation_reads == 0
    assert CountingToolInvocation.trace_reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_kind", ["observation", "trace"])
async def test_nested_tool_model_subclass_is_rejected_without_field_access(
    nested_kind: str,
) -> None:
    class NestedSubclassResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            if nested_kind == "observation":
                nested_observation = CountingObservation.model_validate(
                    invocation.observation.model_dump(mode="python")
                )
                candidate = ToolInvocation.model_construct(
                    observation=nested_observation,
                    trace=invocation.trace,
                )
                CountingObservation.field_reads = 0
            else:
                nested_trace = CountingToolCallTrace.model_validate(
                    invocation.trace.model_dump(mode="python")
                )
                candidate = ToolInvocation.model_construct(
                    observation=invocation.observation,
                    trace=nested_trace,
                )
                CountingToolCallTrace.field_reads = 0
            return candidate

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, (query_result(),))
    context = replace_context(context, tools=NestedSubclassResultTools(tools.registry))

    state = cast(
        AgentState,
        await build_agent_graph().ainvoke(
            new_agent_state(run_id=f"nested-subclass-{nested_kind}", query=QUERY),
            context=context,
        ),
    )

    assert_one_safe_execute_failure(state, events, execute_calls=1)
    if nested_kind == "observation":
        assert CountingObservation.field_reads == 0
    else:
        assert CountingToolCallTrace.field_reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["query_id", "columns", "rows", "row_count", "malformed_column"],
)
async def test_malformed_tool_fields_are_rejected_without_coercion_warning_or_leak(
    field: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = malformed_tool_invocation(field)

    class MalformedResultTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            del node
            self.calls.append(cast(AgentAction, action).action_type)
            return candidate

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, ())
    context = replace_context(context, tools=MalformedResultTools(tools.registry))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        state = cast(
            AgentState,
            await build_agent_graph().ainvoke(
                new_agent_state(run_id=f"malformed-tool-{field}", query=QUERY),
                context=context,
            ),
        )

    assert_one_safe_execute_failure(state, events, execute_calls=1)
    assert state["evidence"] == ()
    assert caught == []
    assert MALFORMED_FIELD_SENTINEL not in caplog.text
    answer = state["final_answer"]
    assert answer is not None
    rendered = json.dumps(
        {
            "answer": answer.model_dump(mode="json"),
            "observations": [item.model_dump(mode="json") for item in state["observations"]],
            "traces": [item.model_dump(mode="json") for item in state["tool_call_traces"]],
            "events": events.items,
        }
    )
    assert MALFORMED_FIELD_SENTINEL not in rendered


@pytest.mark.asyncio
async def test_tool_payload_is_an_owned_snapshot_after_validation_and_run() -> None:
    backing: dict[str, object] = {}

    class AliasedPayloadTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            payload = invocation.observation.payload
            assert isinstance(payload, Mapping)
            backing.update(payload)
            fields = dict(object.__getattribute__(invocation.observation, "__dict__"))
            fields["payload"] = MappingProxyType(backing)
            return ToolInvocation.model_construct(
                observation=Observation.model_construct(**fields),
                trace=invocation.trace,
            )

    scripts = scripts_for(
        QUERY,
        plan=simple_plan(),
        actions=(execute_action(),),
        synthesis_output=synthesis(),
    )
    context, tools, _, events, _ = context_for(scripts, (query_result(),))
    context = replace_context(context, tools=AliasedPayloadTools(tools.registry))

    result = await run_agent(run_id="owned-payload", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.evidence[0].numeric_value == Decimal("125.00")
    backing["rows"] = (("999.00",),)
    backing["secret_token"] = MAPPING_READ_SENTINEL

    execute_observation = next(
        item for item in result.observations if item.tool_name is ActionType.EXECUTE_SQL
    )
    assert isinstance(execute_observation.payload, Mapping)
    assert execute_observation.payload["rows"] == (("125.00",),)
    assert result.evidence[0].numeric_value == Decimal("125.00")
    rendered = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
        }
    )
    assert "999.00" not in rendered
    assert MAPPING_READ_SENTINEL not in rendered


@pytest.mark.asyncio
async def test_tool_payload_mapping_read_failure_is_fixed_and_does_not_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exploding_payload = MappingProxyType(ExplodingMapping())

    class ExplodingPayloadTools(RecordingTools):
        async def invoke(self, action: object, *, node: str):  # type: ignore[no-untyped-def]
            invocation = await super().invoke(action, node=node)
            fields = dict(object.__getattribute__(invocation.observation, "__dict__"))
            fields["payload"] = exploding_payload
            return ToolInvocation.model_construct(
                observation=Observation.model_construct(**fields),
                trace=invocation.trace,
            )

    scripts = scripts_for(QUERY, plan=simple_plan(), actions=(execute_action(),))
    context, tools, _, events, _ = context_for(scripts, (query_result(),))
    context = replace_context(context, tools=ExplodingPayloadTools(tools.registry))

    result = await run_agent(run_id="exploding-payload", query=QUERY, context=context)

    assert result.final_answer.status is FinalStatus.INTERNAL_ERROR
    assert result.observations[-1].safe_error == "internal_tool_error"
    rendered = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "trace": result.safe_trace.model_dump(mode="json"),
            "events": events.items,
            "logs": caplog.text,
        }
    )
    assert MAPPING_READ_SENTINEL not in rendered


@pytest.mark.asyncio
async def test_plan_request_supplies_the_hypothesis_contract_needed_for_execution() -> None:
    scripts = scripts_for(QUERY, actions=(execute_action(),), synthesis_output=synthesis())
    context, _, _, _, _ = context_for(scripts, (query_result(),))

    class ContractFollowingModel(RecordingModel):
        async def invoke(self, request, output_type):  # type: ignore[no-untyped-def]
            if request.purpose != "plan":
                return await self.delegate.invoke(request, output_type)
            plan = simple_plan()
            rules = request.model_dump(mode="json")["user_payload"].get("planning_contract", {})
            metrics = request.model_dump(mode="json")["user_payload"]["metrics"]
            paid = next(item for item in metrics if item["metric_id"] == "paid_gmv")
            assert paid["name_zh"] == "已支付交易额"
            assert paid["name_en"] == "Paid GMV"
            plan["hypotheses"] = rules.get(
                "simple_hypotheses", ({"hypothesis_id": "h1", "kind": "metric_value"},)
            )
            return StructuredModelResult(
                output=output_type.model_validate(plan),
                provider_model=self.model,
                usage=ModelUsage(input_tokens=0, output_tokens=0),
                latency_ms=0,
            )

    result = await run_agent(
        run_id="plan-contract",
        query=QUERY,
        context=replace_context(context, model=ContractFollowingModel(scripts)),
    )
    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.governance.execute_calls == 1


@pytest.mark.asyncio
async def test_action_request_carries_the_retrieved_metric_and_schema_for_sql_generation() -> None:
    scripts = scripts_for(
        QUERY, plan=simple_plan(), actions=(execute_action(),), synthesis_output=synthesis()
    )
    context, _, model, _, _ = context_for(scripts, (query_result(),))
    result = await run_agent(run_id="action-context", query=QUERY, context=context)
    assert result.final_answer.status is FinalStatus.COMPLETED
    request = next(call for call in model.calls if call.purpose == "action")
    payload = request.model_dump(mode="json")["user_payload"]
    metrics = payload.get("metrics", ())
    gmv = next((item for item in metrics if item["metric_id"] == "gmv"), None)
    assert gmv is not None
    assert gmv["expression_sql"]
    assert gmv["default_filters"]
    assert any(table["name"] == "orders" for table in payload.get("tables", ()))
    assert payload["plan"]["windows"]
    assert payload["execute_arguments_schema"]["properties"]["sql"]
    contract = payload["contracts"][0]["validation_contract"]
    assert contract["columns"][0]["nullable"] is False
    assert contract["max_rows"] == 1
    assert contract["order_by"] == []
