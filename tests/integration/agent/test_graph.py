from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from time import monotonic
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from governed_analytics.agent import (
    ActionType,
    AgentContext,
    AgentRunResult,
    FinalStatus,
    JsonValue,
    Observation,
    StopReason,
    run_agent,
)
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.tool_registry import ToolRegistry
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings
from governed_analytics.models.agent_fixtures import AgentScripts, ScriptedAgentModel
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.pricing import ModelPricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits
from governed_analytics.tools import (
    AsyncEngineSqlExecutionBackend,
    ExecuteSqlTool,
    MetricTool,
    ProfileTool,
    SchemaTool,
)

SIMPLE_GMV_QUERY = "2026年6月GMV是多少？"  # noqa: RUF001
ATTRIBUTION_QUERY = "比较六月前两周GMV，并按区域、SKU、客户分群解释下降。"  # noqa: RUF001
PREMISE_NOT_MET_QUERY = "比较五月后两周GMV，并在没有下降时停止归因。"  # noqa: RUF001
_AGENT_DEADLINE_SECONDS = 8.0

SIMPLE_GMV_SQL = """
select coalesce(sum(oi.net_amount), 0) as gmv
from orders as o
join order_items as oi on oi.order_id = o.order_id
where o.ordered_at >= cast(:start_at as timestamptz)
  and o.ordered_at < cast(:end_at as timestamptz)
  and o.status in ('paid', 'completed', 'refunded')
"""

# These four statements are deliberately local to this integration checkpoint.  They are
# candidate SQL consumed by the real Registry/backend, not Oracle or expected-result fixtures.
GMV_COMPARISON_SQL = """
select
  coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:current_start as timestamptz)
      and o.ordered_at < cast(:current_end as timestamptz)
  ), 0) as current_gmv,
  coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:previous_start as timestamptz)
      and o.ordered_at < cast(:previous_end as timestamptz)
  ), 0) as previous_gmv,
  (
    coalesce(sum(oi.net_amount) filter (
      where o.ordered_at >= cast(:current_start as timestamptz)
        and o.ordered_at < cast(:current_end as timestamptz)
    ), 0)
    - coalesce(sum(oi.net_amount) filter (
      where o.ordered_at >= cast(:previous_start as timestamptz)
        and o.ordered_at < cast(:previous_end as timestamptz)
    ), 0)
  ) / nullif(coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:previous_start as timestamptz)
      and o.ordered_at < cast(:previous_end as timestamptz)
  ), 0), 0) as change_rate
from orders as o
join order_items as oi on oi.order_id = o.order_id
where o.status in ('paid', 'completed', 'refunded')
"""

REGION_ATTRIBUTION_SQL = """
select
  o.region as region,
  coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:previous_start as timestamptz)
      and o.ordered_at < cast(:previous_end as timestamptz)
  ), 0) - coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:current_start as timestamptz)
      and o.ordered_at < cast(:current_end as timestamptz)
  ), 0) as gmv_loss
from orders as o
join order_items as oi on oi.order_id = o.order_id
where o.status in ('paid', 'completed', 'refunded')
  and (
    (
      o.ordered_at >= cast(:previous_start as timestamptz)
      and o.ordered_at < cast(:previous_end as timestamptz)
    ) or (
      o.ordered_at >= cast(:current_start as timestamptz)
      and o.ordered_at < cast(:current_end as timestamptz)
    )
  )
group by o.region
order by gmv_loss desc, region asc
fetch first 10 rows only
"""

SKU_ATTRIBUTION_SQL = """
select
  p.sku as sku,
  coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:previous_start as timestamptz)
      and o.ordered_at < cast(:previous_end as timestamptz)
  ), 0) - coalesce(sum(oi.net_amount) filter (
    where o.ordered_at >= cast(:current_start as timestamptz)
      and o.ordered_at < cast(:current_end as timestamptz)
  ), 0) as gmv_loss
from orders as o
join order_items as oi on oi.order_id = o.order_id
join products as p on p.product_id = oi.product_id
where o.status in ('paid', 'completed', 'refunded')
  and (
    (
      o.ordered_at >= cast(:previous_start as timestamptz)
      and o.ordered_at < cast(:previous_end as timestamptz)
    ) or (
      o.ordered_at >= cast(:current_start as timestamptz)
      and o.ordered_at < cast(:current_end as timestamptz)
    )
  )
group by p.sku
order by gmv_loss desc, sku asc
fetch first 10 rows only
"""

SEGMENT_ATTRIBUTION_SQL = """
with segment_gmv as (
  select
    c.segment as segment,
    coalesce(sum(oi.net_amount) filter (
      where o.ordered_at >= cast(:previous_start as timestamptz)
        and o.ordered_at < cast(:previous_end as timestamptz)
    ), 0) as previous_gmv,
    coalesce(sum(oi.net_amount) filter (
      where o.ordered_at >= cast(:current_start as timestamptz)
        and o.ordered_at < cast(:current_end as timestamptz)
    ), 0) as current_gmv
  from orders as o
  join order_items as oi on oi.order_id = o.order_id
  join customers as c on c.customer_id = o.customer_id
  where o.status in ('paid', 'completed', 'refunded')
    and (
      (
        o.ordered_at >= cast(:previous_start as timestamptz)
        and o.ordered_at < cast(:previous_end as timestamptz)
      ) or (
        o.ordered_at >= cast(:current_start as timestamptz)
        and o.ordered_at < cast(:current_end as timestamptz)
      )
    )
  group by c.segment
)
select segment, previous_gmv, current_gmv, current_gmv - previous_gmv as delta
from segment_gmv
order by delta asc, segment asc
fetch first 10 rows only
"""

JUNE_PARAMETERS = {
    "previous_start": "2026-06-01T00:00:00Z",
    "previous_end": "2026-06-08T00:00:00Z",
    "current_start": "2026-06-08T00:00:00Z",
    "current_end": "2026-06-15T00:00:00Z",
}
MAY_PARAMETERS = {
    "previous_start": "2026-05-18T00:00:00Z",
    "previous_end": "2026-05-25T00:00:00Z",
    "current_start": "2026-05-25T00:00:00Z",
    "current_end": "2026-06-01T00:00:00Z",
}


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()


class _RecordingEvents:
    def __init__(self) -> None:
        self.types: list[str] = []

    async def emit(
        self,
        node: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        del node, data
        self.types.append(event_type)


def _fixture_pricing() -> ModelPricing:
    return ModelPricing.model_validate(
        {
            "provider": "fixture",
            "region": "local",
            "requested_model": "fixture-agent",
            "resolved_model": "fixture-agent",
            "effective_date": date(2026, 9, 4),
            "currency": "CNY",
            "unit_tokens": 1,
            "input_token_upper_bound": 1_000_000,
            "input_price": "0",
            "output_price": "0",
            "pricing_basis": "integration test",
            "source": "https://example.invalid/fixture-pricing",
        }
    )


def _execute_action(
    contract_id: str,
    hypothesis_id: str,
    sql: str,
    parameters: Mapping[str, str],
) -> dict[str, object]:
    return {
        "action_type": "execute_sql",
        "purpose": contract_id,
        "arguments": {"sql": sql, "parameters": dict(parameters)},
        "hypothesis_id": hypothesis_id,
        "contract_id": contract_id,
        "expected_evidence": "contracted numeric result",
    }


def _attribution_plan(parameters: Mapping[str, str]) -> dict[str, object]:
    return {
        "plan_id": "gmv-attribution-integration",
        "revision": 1,
        "metric_id": "gmv",
        "metric_version": "1.0.0",
        "analysis_type": "attribution",
        "windows": (
            {
                "label": "previous",
                "start_at": parameters["previous_start"],
                "end_at": parameters["previous_end"],
            },
            {
                "label": "current",
                "start_at": parameters["current_start"],
                "end_at": parameters["current_end"],
            },
        ),
        "dimensions": ("region", "product", "segment"),
        "null_policy": "preserve",
        "zero_denominator_policy": "return_null",
        "fill_policy": "none",
        "sort": ({"column": "gmv_loss", "direction": "desc"},),
        "top_k": 10,
        "tie_break": ("region",),
        "hypotheses": (
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
        ),
    }


def _synthesis(stop_reason: str) -> dict[str, object]:
    return {
        "status": "completed",
        "stop_reason": stop_reason,
        "answer": "GMV 数据库链路验证完成。",
        "evidence_ids": (),
    }


SIMPLE_SCRIPTS = cast(
    AgentScripts,
    {
        SIMPLE_GMV_QUERY.casefold(): {
            "behavior": (
                {
                    "action": "execute",
                    "reason_code": "ready",
                    "missing_fields": (),
                    "user_message": "开始分析。",
                },
            ),
            "plan": (
                {
                    "plan_id": "simple-gmv-integration",
                    "revision": 1,
                    "metric_id": "gmv",
                    "metric_version": "1.0.0",
                    "analysis_type": "simple",
                    "windows": (
                        {
                            "label": "current",
                            "start_at": "2026-06-01T00:00:00Z",
                            "end_at": "2026-07-01T00:00:00Z",
                        },
                    ),
                    "null_policy": "preserve",
                    "zero_denominator_policy": "return_null",
                    "fill_policy": "none",
                    "hypotheses": ({"hypothesis_id": "metric_value", "kind": "metric_value"},),
                },
            ),
            "action": (
                _execute_action(
                    "metric_value_contract",
                    "metric_value",
                    SIMPLE_GMV_SQL,
                    {
                        "start_at": "2026-06-01T00:00:00Z",
                        "end_at": "2026-07-01T00:00:00Z",
                    },
                ),
            ),
            "synthesis": (_synthesis("answer_complete"),),
        }
    },
)


def _attribution_scripts(
    *, query: str, parameters: Mapping[str, str], include_dimensions: bool
) -> AgentScripts:
    actions: list[dict[str, object]] = [
        _execute_action(
            "gmv_comparison",
            "confirm_decline",
            GMV_COMPARISON_SQL,
            parameters,
        )
    ]
    if include_dimensions:
        actions.extend(
            (
                _execute_action(
                    "region_contribution",
                    "region_contribution",
                    REGION_ATTRIBUTION_SQL,
                    parameters,
                ),
                _execute_action(
                    "sku_contribution",
                    "sku_contribution",
                    SKU_ATTRIBUTION_SQL,
                    parameters,
                ),
                _execute_action(
                    "segment_contribution",
                    "segment_contribution",
                    SEGMENT_ATTRIBUTION_SQL,
                    parameters,
                ),
            )
        )
    return cast(
        AgentScripts,
        {
            query.casefold(): {
                "behavior": (
                    {
                        "action": "execute",
                        "reason_code": "ready",
                        "missing_fields": (),
                        "user_message": "开始分析。",
                    },
                ),
                "plan": (_attribution_plan(parameters),),
                "action": tuple(actions),
                "synthesis": (
                    _synthesis("answer_complete" if include_dimensions else "premise_not_met"),
                ),
            }
        },
    )


ATTRIBUTION_SCRIPTS = _attribution_scripts(
    query=ATTRIBUTION_QUERY,
    parameters=JUNE_PARAMETERS,
    include_dimensions=True,
)
PREMISE_NOT_MET_SCRIPTS = _attribution_scripts(
    query=PREMISE_NOT_MET_QUERY,
    parameters=MAY_PARAMETERS,
    include_dimensions=False,
)


async def _run_fixture(query: str, scripts: AgentScripts, run_id: str) -> AgentRunResult:
    database = DatabaseSettings()  # type: ignore[call-arg]
    engine = create_async_database_engine(database)
    try:
        await _assert_readonly_database_identity(engine)
        backend = AsyncEngineSqlExecutionBackend(engine)
        registry = ToolRegistry.default(
            SchemaTool(),
            MetricTool(),
            ProfileTool(backend=backend),
            ExecuteSqlTool(backend=backend),
        )
        settings = AgentRuntimeSettings(_env_file=None)  # type: ignore[call-arg]
        clock = _Clock()
        recorder = InMemoryTraceRecorder()
        budget = BudgetLedger(
            limits=BudgetLimits.from_settings(settings),
            pricing=_fixture_pricing(),
            monotonic=clock.monotonic,
        )
        context = AgentContext(
            model_invoker=StructuredModelInvoker(
                ScriptedAgentModel(scripts), budget, recorder, clock
            ),
            tools=registry,
            budget=budget,
            events=_RecordingEvents(),
            trace_recorder=recorder,
            clock=clock,
        )
        try:
            async with asyncio.timeout(_AGENT_DEADLINE_SECONDS):
                return await run_agent(run_id=run_id, query=query, context=context)
        except TimeoutError:
            raise AssertionError("agent integration exceeded deterministic test deadline") from None
    finally:
        await engine.dispose()


async def _assert_readonly_database_identity(engine: AsyncEngine) -> None:
    async with engine.connect() as connection, connection.begin():
        await connection.execute(text("set transaction isolation level repeatable read, read only"))
        proof = await connection.execute(
            text(
                """
                select current_user, current_setting('transaction_read_only'),
                  has_table_privilege(current_user, 'public.orders', 'SELECT'),
                  has_table_privilege(current_user, 'public.orders', 'INSERT'),
                  has_table_privilege(current_user, 'public.orders', 'UPDATE'),
                  has_table_privilege(current_user, 'public.orders', 'DELETE'),
                  has_schema_privilege(current_user, 'public', 'CREATE')
                """
            )
        )
        assert proof.one() == (
            "analytics_readonly",
            "on",
            True,
            False,
            False,
            False,
            False,
        )


async def run_metric_fixture(metric_id: str) -> AgentRunResult:
    assert metric_id == "gmv"
    return await _run_fixture(SIMPLE_GMV_QUERY, SIMPLE_SCRIPTS, "integration-simple-gmv")


def _execute_observations(result: AgentRunResult) -> tuple[Observation, ...]:
    return tuple(item for item in result.observations if item.tool_name is ActionType.EXECUTE_SQL)


def _result_decimal(observation: Observation, column: str) -> Decimal:
    payload = observation.payload
    assert isinstance(payload, Mapping)
    columns = payload["columns"]
    rows = payload["rows"]
    assert isinstance(columns, tuple)
    assert isinstance(rows, tuple) and len(rows) == 1
    row = rows[0]
    assert isinstance(row, tuple)
    return Decimal(str(row[columns.index(column)]))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gmv_runs_through_agent_registry_and_real_backend() -> None:
    result = await run_metric_fixture("gmv")

    assert result.final_answer.status is FinalStatus.COMPLETED
    execute = _execute_observations(result)
    assert len(execute) == 1
    observation = result.observations[-1]
    assert observation.contract_id == "metric_value_contract"
    assert observation.query_id is not None and len(observation.query_id) == 64
    assert not observation.possibly_truncated
    assert observation.columns == ("gmv",)
    assert observation.row_count == 1
    assert _result_decimal(observation, "gmv") == Decimal("94636.23")
    assert result.observation_validations[0].valid
    assert result.evidence
    assert {item.query_id for item in result.evidence} == {observation.query_id}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gmv_attribution_runs_comparison_region_sku_and_segment() -> None:
    result = await _run_fixture(
        ATTRIBUTION_QUERY,
        ATTRIBUTION_SCRIPTS,
        "integration-gmv-attribution",
    )

    execute = tuple(
        item for item in result.observations if item.tool_name is ActionType.EXECUTE_SQL
    )
    contract_ids = (
        "gmv_comparison",
        "region_contribution",
        "sku_contribution",
        "segment_contribution",
    )
    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.final_answer.stop_reason is StopReason.ANSWER_COMPLETE
    assert tuple(item.contract_id for item in execute) == contract_ids
    assert tuple(item.contract_id for item in result.observation_validations) == contract_ids
    assert all(item.valid for item in result.observation_validations)
    query_ids = tuple(item.query_id for item in execute)
    assert len(set(query_ids)) == 4
    assert all(query_id is not None and len(query_id) == 64 for query_id in query_ids)
    evidence_contracts = {item.contract_id for item in result.evidence if item.verified}
    assert evidence_contracts == set(contract_ids)
    for observation in execute:
        assert any(
            evidence.query_id == observation.query_id
            and evidence.contract_id == observation.contract_id
            and evidence.verified
            for evidence in result.evidence
        )
    assert result.governance.execute_calls == result.governance.action_loops == 4
    comparison = execute[0]
    assert _result_decimal(comparison, "current_gmv") == Decimal("17893.50")
    assert _result_decimal(comparison, "previous_gmv") == Decimal("50873.80")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_current_at_least_previous_stops_after_comparison() -> None:
    result = await _run_fixture(
        PREMISE_NOT_MET_QUERY,
        PREMISE_NOT_MET_SCRIPTS,
        "integration-gmv-premise-not-met",
    )

    execute = tuple(
        item for item in result.observations if item.tool_name is ActionType.EXECUTE_SQL
    )
    assert result.final_answer.status is FinalStatus.COMPLETED
    assert result.final_answer.stop_reason is StopReason.PREMISE_NOT_MET
    assert tuple(item.contract_id for item in execute) == ("gmv_comparison",)
    assert result.governance.execute_calls == result.governance.action_loops == 1
    assert _result_decimal(execute[0], "current_gmv") == Decimal("15473.88")
    assert _result_decimal(execute[0], "previous_gmv") == Decimal("9497.32")
    comparison_evidence = tuple(
        item for item in result.evidence if item.contract_id == "gmv_comparison"
    )
    assert comparison_evidence
    assert all(item.stance == "refutes" for item in comparison_evidence)
