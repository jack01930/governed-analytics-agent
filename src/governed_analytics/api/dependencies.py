"""Lifespan-owned runtime composition for the Agent API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import monotonic

from openai import AsyncOpenAI

from governed_analytics.agent.graph import build_agent_graph
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.ports import AgentContext, AgentModel
from governed_analytics.agent.tool_registry import ToolRegistry
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.models.agent_fixtures import (
    ScriptedAgentModel,
    builtin_demo_scripts,
)
from governed_analytics.models.agent_openai_compatible import OpenAICompatibleAgentModel
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.pricing import ModelPricing, load_model_pricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits
from governed_analytics.runtime.events import BoundEventSink, EventStore, InMemoryEventStore
from governed_analytics.runtime.runs import AnalysisRunner, InMemoryRunStore
from governed_analytics.tools.execution import AsyncEngineSqlExecutionBackend
from governed_analytics.tools.tools import ExecuteSqlTool, MetricTool, ProfileTool, SchemaTool

_LIVE_PRICING_PATH = "data/pricing/deepseek-v4-flash-2026-09-01.yaml"


class AgentStartupError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("agent startup failed")


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()


@dataclass(frozen=True, slots=True)
class AppContainer:
    settings: AgentRuntimeSettings
    runner: AnalysisRunner
    events: EventStore


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
            "pricing_basis": "deterministic fixture",
            "source": "https://example.invalid/fixture-pricing",
        }
    )


@asynccontextmanager
async def build_container(
    *,
    runtime_settings: AgentRuntimeSettings | None = None,
    database_settings: DatabaseSettings | None = None,
    model_settings: ModelSettings | None = None,
) -> AsyncIterator[AppContainer]:
    engine = None
    client: AsyncOpenAI | None = None
    runner: AnalysisRunner | None = None

    async def cleanup() -> None:
        if runner is not None:
            with suppress(Exception):
                await runner.shutdown()
        if client is not None:
            with suppress(Exception):
                await client.close()
        if engine is not None:
            with suppress(Exception):
                await engine.dispose()

    try:
        settings = runtime_settings or AgentRuntimeSettings()
        database = database_settings or DatabaseSettings()  # type: ignore[call-arg]
        model_configuration = model_settings or ModelSettings()
        engine = create_async_database_engine(database)
        backend = AsyncEngineSqlExecutionBackend(engine)
        metric_tool = MetricTool()
        if not metric_tool.list().ok:
            raise AgentStartupError()
        registry = ToolRegistry.default(
            SchemaTool(),
            metric_tool,
            ProfileTool(backend=backend),
            ExecuteSqlTool(backend=backend),
        )
        _ = build_agent_graph()

        scripts = builtin_demo_scripts()
        shared_model: AgentModel | None = None
        if settings.runtime_mode == "fixture":
            pricing = _fixture_pricing()
        else:
            pricing = load_model_pricing(_LIVE_PRICING_PATH)
            if (
                not settings.live_enabled
                or model_configuration.model_api_key is None
                or pricing.requested_model != model_configuration.model_name
            ):
                raise AgentStartupError()
            client = AsyncOpenAI(
                api_key=model_configuration.model_api_key.get_secret_value(),
                base_url=model_configuration.model_base_url,
                max_retries=0,
            )
            shared_model = OpenAICompatibleAgentModel(
                client,
                model_configuration.model_name,
            )

        events = InMemoryEventStore()
        runs = InMemoryRunStore(
            max_runs=settings.max_runs,
            retention_seconds=settings.run_retention_seconds,
        )
        clock = SystemClock()
        limits = BudgetLimits.from_settings(settings)

        def context_factory(run_id: str, owner_token: object) -> AgentContext:
            model = (
                ScriptedAgentModel(scripts, unsupported_fallback=True)
                if shared_model is None
                else shared_model
            )
            recorder = InMemoryTraceRecorder()
            budget = BudgetLedger(
                limits=limits,
                pricing=pricing,
                monotonic=clock.monotonic,
            )
            return AgentContext(
                model_invoker=StructuredModelInvoker(model, budget, recorder, clock),
                tools=registry,
                budget=budget,
                events=BoundEventSink(events, run_id, owner_token=owner_token),
                trace_recorder=recorder,
                clock=clock,
            )

        runner = AnalysisRunner(
            settings=settings,
            runs=runs,
            events=events,
            context_factory=context_factory,
        )
    except asyncio.CancelledError:
        await cleanup()
        raise
    except AgentStartupError:
        await cleanup()
        raise
    except Exception:
        await cleanup()
        raise AgentStartupError() from None

    try:
        yield AppContainer(settings=settings, runner=runner, events=events)
    finally:
        await cleanup()


__all__ = ["AgentStartupError", "AppContainer", "SystemClock", "build_container"]
