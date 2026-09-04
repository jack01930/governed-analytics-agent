from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from langgraph.runtime import Runtime

from governed_analytics.agent import (
    ActionType,
    AgentAction,
    AgentContext,
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    ModelPurpose,
    StopReason,
    StructuredModelRequest,
    TypedMetricPlan,
    build_agent_graph,
    new_agent_state,
    run_agent,
)
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.nodes.behavior import map_agent_failure
from governed_analytics.agent.nodes.execution import route_action
from governed_analytics.agent.nodes.planning import compile_answer_contract
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

    def consume_repair(self) -> GovernanceSnapshot:
        self.delegate.consume_repair()
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
            "input_token_upper_bound": 10000,
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
) -> BudgetLedger:
    return BudgetLedger(
        limits=BudgetLimits(
            max_action_loops=4,
            max_llm_calls=max_llm_calls,
            max_tool_calls=max_tool_calls,
            max_execute_calls=5,
            max_profile_calls=2,
            max_repairs=1,
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
) -> QueryResult:
    return QueryResult(
        query_id=query_id,
        columns=columns,
        rows=rows,
        row_count=len(rows),
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
async def test_attribution_checks_decline_then_three_dimensions() -> None:
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
        query_result(("region", "gmv_loss"), (("north", "12"),), query_id="2" * 64),
        query_result(("sku", "gmv_loss"), (("sku-1", "9"),), query_id="3" * 64),
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
    context, _, _, _, _ = context_for(scripts, results)

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
        actions=(execute_action(sql="select cast(125 as numeric) as value"),),
        repairs=(execute_action(sql="drop table orders"),),
    )
    context, tools, _, _, backend = context_for(
        scripts,
        (query_result(("value",), (("125",),), query_id="c" * 64),),
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
        actions=(execute_action(sql="select cast(125 as numeric) as value"),),
        repairs=({"not": "an action"},),
    )
    context, tools, model, _, backend = context_for(
        scripts,
        (query_result(("value",), (("125",),), query_id="c" * 64),),
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
    assert tuple(item.tool_name for item in result.observations) == (
        ActionType.METRIC_LOOKUP,
    )
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
        "select private_row_sentinel from orders; "
        "sk-private-key https://private-endpoint.invalid"
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
    assert map_agent_failure(TimeoutError("raw timeout sentinel")) is StopReason.SQL_TIMEOUT
    assert map_agent_failure(RuntimeError("raw exception sentinel")) is StopReason.INTERNAL_ERROR
