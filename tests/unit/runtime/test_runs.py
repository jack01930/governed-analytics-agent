from __future__ import annotations

import asyncio
import gc
import json
import warnings
from collections.abc import AsyncGenerator, Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import BaseModel

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    EvidenceItem,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    Observation,
    ObservationValidation,
    SafeTrace,
    StopReason,
    StructuredModelRequest,
    StructuredModelResult,
)
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.ports import AgentContext
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.pricing import ModelPricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits
from governed_analytics.runtime.events import InMemoryEventStore, RunEvent
from governed_analytics.runtime.events import RunNotFound as EventRunNotFound
from governed_analytics.runtime.runs import (
    AnalysisRunner,
    InMemoryRunStore,
    RunAlreadyExists,
    RunCapacityExceeded,
    RunConsistencyError,
    RunnerShutdown,
    RunNotFound,
    RunOwnershipMismatch,
    RunStateConflict,
    RunSubmissionFailed,
)

SENTINEL = "raw-row-secret-sentinel"


@pytest.mark.asyncio
async def test_run_store_conditions_generation_operations_on_exact_owner_token() -> None:
    clock = FakeClock()
    store = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    owner = object()
    foreign = object()
    created = await store.create("run-1", "query", owner_token=owner)

    with pytest.raises(RunOwnershipMismatch, match="generation ownership mismatch"):
        await store.get("run-1", owner_token=foreign)
    with pytest.raises(RunOwnershipMismatch, match="generation ownership mismatch"):
        await store.delete_queued("run-1", owner_token=foreign)

    assert await store.get("run-1", owner_token=owner) == created
    assert "owner_token" not in created.model_dump_json()


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 9, 4, tzinfo=UTC)
        self.monotonic_seconds = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.monotonic_seconds += seconds


def completed_result(
    run_id: str,
    *,
    governance: GovernanceSnapshot | None = None,
    trace: SafeTrace | None = None,
) -> AgentRunResult:
    return AgentRunResult(
        run_id=run_id,
        behavior=None,
        answer_contract=None,
        observations=(),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=None,
        repair_history=(),
        governance=governance or GovernanceSnapshot(),
        final_answer=FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer="分析完成。",
        ),
        safe_trace=trace or SafeTrace(),
    )


def result_with_raw_observation(run_id: str, payload: JsonValue) -> AgentRunResult:
    query_id = "a" * 64
    observation = Observation(
        observation_id="observation-1",
        tool_name=ActionType.EXECUTE_SQL,
        purpose="contract-1",
        ok=True,
        hypothesis_id="hypothesis-1",
        contract_id="contract-1",
        query_id=query_id,
        columns=("gmv",),
        row_count=1,
        payload=payload,
    )
    return AgentRunResult(
        run_id=run_id,
        behavior=None,
        answer_contract=None,
        observations=(observation,),
        observation_validations=(
            ObservationValidation(
                observation_id="observation-1",
                contract_id="contract-1",
                validation_fingerprint="b" * 64,
                valid=True,
            ),
        ),
        evidence=(
            EvidenceItem(
                evidence_id="evidence-1",
                observation_id="observation-1",
                hypothesis_id="hypothesis-1",
                contract_id="contract-1",
                query_id=query_id,
                claim_key="gmv",
                stance="supports",
                numeric_value=Decimal("125"),
                unit="CNY",
                verified=True,
                limitations=("fixture only",),
            ),
        ),
        evidence_gaps=("regional split unavailable",),
        first_candidate=observation,
        repair_history=(),
        governance=GovernanceSnapshot(llm_calls=1),
        final_answer=FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer="GMV 为 125 元。",
            evidence_ids=("evidence-1",),
            limitations=("fixture only",),
        ),
        safe_trace=SafeTrace(),
    )


@pytest.mark.asyncio
async def test_run_store_never_overwrites_and_evicts_only_expired_terminal_runs() -> None:
    clock = FakeClock()
    store = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)

    await store.create("run-1", "query one")
    await store.create("run-2", "query two")
    with pytest.raises(RunCapacityExceeded):
        await store.create("run-3", "query three")
    with pytest.raises(RunAlreadyExists):
        await store.create("run-1", "replacement")
    await store.mark_running("run-1")
    await store.complete("run-1", completed_result("run-1"))
    clock.advance(3599)
    assert await store.expired_terminal_ids() == ()
    clock.advance(1)
    assert await store.expired_terminal_ids() == ("run-1",)
    assert (await store.get("run-1")).lifecycle_status == "terminal"
    assert await store.delete_terminal("run-1") is True
    assert (await store.get("run-2")).lifecycle_status == "queued"


@pytest.mark.asyncio
async def test_store_enforces_strict_lifecycle_and_exact_deletion_states() -> None:
    store = InMemoryRunStore(max_runs=4, retention_seconds=1, clock=FakeClock())
    await store.create("queued", "q")
    await store.create("running", "q")
    await store.mark_running("running")

    with pytest.raises(RunStateConflict):
        await store.complete("queued", completed_result("queued"))
    with pytest.raises(RunStateConflict):
        await store.mark_running("running")
    assert await store.delete_terminal("queued") is False
    assert await store.delete_queued("running") is False
    assert await store.delete_queued("queued") is True
    with pytest.raises(RunNotFound):
        await store.get("queued")


@pytest.mark.asyncio
async def test_store_complete_is_idempotent_only_for_the_same_projection() -> None:
    store = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=FakeClock())
    await store.create("run-1", "query")
    await store.mark_running("run-1")
    result = completed_result("run-1")

    first = await store.complete("run-1", result)
    assert await store.complete("run-1", result) == first
    different = result.model_copy(
        update={
            "final_answer": FinalAnswer(
                status=FinalStatus.COMPLETED,
                stop_reason=StopReason.PREMISE_NOT_MET,
                answer="前提不成立。",
            )
        }
    )
    with pytest.raises(RunStateConflict):
        await store.complete("run-1", different)


@pytest.mark.asyncio
async def test_complete_projects_agent_result_without_observation_payload() -> None:
    store = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=FakeClock())
    payload: dict[str, JsonValue] = {
        "rows": ({"gmv": SENTINEL},),
        "nested": {"raw": SENTINEL},
    }
    result = result_with_raw_observation("run-1", payload)
    await store.create("run-1", "safe query")
    await store.mark_running("run-1")

    record = await store.complete("run-1", result)
    object.__setattr__(result.final_answer, "answer", SENTINEL)
    payload["later"] = SENTINEL

    serialized = record.model_dump_json()
    projected_for_api = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    assert SENTINEL not in serialized
    assert SENTINEL not in projected_for_api
    assert "payload" not in serialized
    assert "rows" not in serialized
    assert record.answer == "GMV 为 125 元。"
    assert tuple(item.evidence_id for item in record.evidence) == ("evidence-1",)
    assert all(item.verified for item in record.evidence)


class ControlledTimeout:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.entered = asyncio.Event()
        self._task: asyncio.Task[object] | None = None
        self._triggered = False

    async def __aenter__(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._task = cast(asyncio.Task[object], task)
        self.entered.set()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc, traceback
        if self._triggered and exc_type is asyncio.CancelledError:
            raise TimeoutError from None
        return False

    def trigger(self) -> None:
        assert self._task is not None
        self._triggered = True
        self._task.cancel()


class ControlledTimeoutFactory:
    def __init__(self) -> None:
        self.contexts: list[ControlledTimeout] = []

    def __call__(self, seconds: float) -> ControlledTimeout:
        context = ControlledTimeout(seconds)
        self.contexts.append(context)
        return context


def settings(**overrides: object) -> AgentRuntimeSettings:
    return AgentRuntimeSettings(  # type: ignore[call-arg]
        _env_file=None,
        **overrides,  # type: ignore[arg-type]
    )


def context_for(clock: FakeClock) -> AgentContext:
    budget = BudgetLedger(
        limits=BudgetLimits.from_settings(settings()),
        pricing=pricing(),
        monotonic=clock.monotonic,
    )
    recorder = InMemoryTraceRecorder()
    return AgentContext(
        model_invoker=cast(Any, object()),
        tools=cast(Any, object()),
        budget=budget,
        events=cast(Any, object()),
        trace_recorder=recorder,
        clock=clock,
    )


class BlockingExecutor:
    def __init__(self) -> None:
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []

    async def __call__(self, *, run_id: str, query: str, context: AgentContext) -> AgentRunResult:
        del query, context
        self.calls.append(run_id)
        self.entered.setdefault(run_id, asyncio.Event()).set()
        await self.release.setdefault(run_id, asyncio.Event()).wait()
        return completed_result(run_id)


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_concurrency_two_queue_time_excluded_and_controlled_timeout() -> None:
    clock = FakeClock()
    run_store = InMemoryRunStore(max_runs=10, retention_seconds=3600, clock=clock)
    event_store = InMemoryEventStore(clock=clock.now)
    executor = BlockingExecutor()
    timeout_factory = ControlledTimeoutFactory()
    context_created: list[tuple[str, float]] = []
    ids = iter(("run-1", "run-2", "run-3"))

    def make_context(run_id: str) -> AgentContext:
        context_created.append((run_id, clock.monotonic()))
        return context_for(clock)

    runner = AnalysisRunner(
        settings=settings(max_concurrent_runs=2),
        runs=run_store,
        events=event_store,
        context_factory=make_context,
        agent_executor=executor,
        timeout_factory=timeout_factory,
        id_factory=lambda: next(ids),
    )
    await runner.submit("one")
    await runner.submit("two")
    await runner.submit("three")
    await wait_until(lambda: len(executor.calls) == 2)

    assert (await run_store.get("run-3")).lifecycle_status == "queued"
    assert [item[0] for item in context_created] == ["run-1", "run-2"]
    assert len(timeout_factory.contexts) == 2
    clock.advance(600)
    assert (await run_store.get("run-3")).lifecycle_status == "queued"

    executor.release["run-1"].set()
    await runner.wait("run-1")
    await wait_until(lambda: "run-3" in executor.calls)
    assert context_created[-1] == ("run-3", 600.0)
    assert len(timeout_factory.contexts) == 3
    timeout_factory.contexts[2].trigger()

    timed_out = await runner.wait("run-3")
    assert timed_out.final_status is FinalStatus.EXECUTION_FAILED
    assert timed_out.stop_reason is StopReason.TASK_TIMEOUT
    executor.release["run-2"].set()
    await runner.wait("run-2")
    await runner.shutdown()


@pytest.mark.asyncio
async def test_cancelled_event_consumer_does_not_cancel_analysis() -> None:
    clock = FakeClock()
    run_store = InMemoryRunStore(max_runs=4, retention_seconds=3600, clock=clock)
    event_store = InMemoryEventStore(clock=clock.now)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(),
        runs=run_store,
        events=event_store,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")
    await wait_until(lambda: "run-1" in executor.calls)

    async def consume() -> None:
        async for _event in event_store.stream("run-1", after_sequence=None):
            await asyncio.Event().wait()

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert (await run_store.get("run-1")).lifecycle_status == "running"

    executor.release["run-1"].set()
    assert (await runner.wait("run-1")).final_status is FinalStatus.COMPLETED
    await runner.shutdown()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_analysis_task() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")
    await wait_until(lambda: "run-1" in executor.calls)
    waiter = asyncio.create_task(runner.wait("run-1"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    executor.release["run-1"].set()
    assert (await runner.wait("run-1")).final_status is FinalStatus.COMPLETED
    await runner.shutdown()


class FailingCreatedEventStore(InMemoryEventStore):
    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if event_type == "run.created":
            raise RuntimeError(f"do not expose {SENTINEL}")
        return await super().emit(run_id, node, event_type, data, owner_token=owner_token)


@pytest.mark.asyncio
async def test_submit_rolls_back_both_stores_when_event_creation_fails() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = FailingCreatedEventStore(clock=clock.now)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "run-1",
    )

    with pytest.raises(RunSubmissionFailed, match="run submission failed") as raised:
        await runner.submit("private query")
    assert SENTINEL not in str(raised.value)
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")
    assert executor.calls == []


class RecordingEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.created: list[str] = []

    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        self.created.append(run_id)
        await super().create_run(run_id, owner_token=owner_token)


class BlockingPreflightEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.preflight_started = asyncio.Event()

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        self.preflight_started.set()
        await asyncio.Event().wait()
        return await super().high_water_mark(run_id, owner_token=owner_token)


@pytest.mark.asyncio
async def test_submit_cancellation_during_event_preflight_removes_owned_run() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = BlockingPreflightEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.preflight_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await InMemoryEventStore.high_water_mark(events, "run-1")


@pytest.mark.asyncio
async def test_capacity_failure_creates_no_event_or_task() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=1, retention_seconds=3600, clock=clock)
    await runs.create("existing", "q")
    events = RecordingEventStore(clock=clock.now)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(max_runs=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "new-run",
    )

    with pytest.raises(RunCapacityExceeded):
        await runner.submit("query")
    assert events.created == []
    assert executor.calls == []


async def add_terminal_pair(
    runs: InMemoryRunStore,
    events: InMemoryEventStore,
    run_id: str,
) -> None:
    await runs.create(run_id, "old")
    await events.create_run(run_id)
    await events.emit(run_id, "runtime", "run.created", {"status": "queued"})
    await runs.mark_running(run_id)
    await events.emit(run_id, "runtime", "run.started", {"status": "running"})
    result = completed_result(run_id)
    await runs.complete(run_id, result)
    await events.emit_terminal(
        run_id,
        {"final_status": "completed", "stop_reason": "answer_complete"},
    )


@pytest.mark.asyncio
async def test_submit_prunes_full_capacity_of_expired_terminal_runs_before_create() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=1, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old-1")
    await add_terminal_pair(runs, events, "old-2")
    clock.advance(1)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "new-run",
    )

    created = await runner.submit("new")
    assert created.run_id == "new-run"
    for old_id in ("old-1", "old-2"):
        with pytest.raises(RunNotFound):
            await runs.get(old_id)
        with pytest.raises(EventRunNotFound):
            await events.high_water_mark(old_id)
    await runner.shutdown()


@pytest.mark.asyncio
async def test_prune_retains_both_stores_while_stream_is_active_then_deletes_both() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=10, retention_seconds=1, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    stream = events.stream("old", after_sequence=None)
    assert (await anext(stream)).type == "run.created"
    executor = BlockingExecutor()
    ids = iter(("new-1", "new-2"))
    runner = AnalysisRunner(
        settings=settings(run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: next(ids),
    )

    await runner.submit("first")
    assert (await runs.get("old")).lifecycle_status == "terminal"
    assert await events.high_water_mark("old") == 3
    await stream.aclose()
    await runner.submit("second")
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")
    await runner.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_work_without_writing_terminal_and_is_idempotent() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=4, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    executor = BlockingExecutor()
    ids = iter(("running", "queued", "rejected"))
    runner = AnalysisRunner(
        settings=settings(max_concurrent_runs=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: next(ids),
    )
    await runner.submit("one")
    await runner.submit("two")
    await wait_until(lambda: executor.calls == ["running"])

    await runner.shutdown()
    await runner.shutdown()

    assert (await runs.get("running")).lifecycle_status == "running"
    assert (await runs.get("queued")).lifecycle_status == "queued"
    assert await events.has_terminal("running") is False
    assert await events.has_terminal("queued") is False
    with pytest.raises(RunnerShutdown):
        await runner.submit("secret query")


class BlockingTerminalEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.terminal_entered = asyncio.Event()
        self.release_terminal = asyncio.Event()

    async def emit_terminal(
        self,
        run_id: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        self.terminal_entered.set()
        await self.release_terminal.wait()
        return await super().emit_terminal(run_id, data, owner_token=owner_token)


@pytest.mark.asyncio
async def test_shutdown_waits_for_started_terminal_write_before_resources_may_close() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = BlockingTerminalEventStore(clock=clock.now)

    async def finish(*, run_id: str, query: str, context: AgentContext) -> AgentRunResult:
        del query, context
        return completed_result(run_id)

    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=finish,
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")
    await events.terminal_entered.wait()

    shutdown = asyncio.create_task(runner.shutdown())
    await asyncio.sleep(0)
    assert shutdown.done() is False
    events.release_terminal.set()
    await shutdown

    assert (await runs.get("run-1")).lifecycle_status == "terminal"
    assert await events.has_terminal("run-1") is True


@pytest.mark.asyncio
async def test_unknown_exception_becomes_one_deterministic_internal_terminal() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)

    async def explode(*, run_id: str, query: str, context: AgentContext) -> AgentRunResult:
        del run_id, query, context
        raise RuntimeError(f"provider raw {SENTINEL}")

    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=explode,
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")
    record = await runner.wait("run-1")

    assert record.final_status is FinalStatus.INTERNAL_ERROR
    assert record.stop_reason is StopReason.INTERNAL_ERROR
    serialized = record.model_dump_json()
    assert SENTINEL not in serialized
    terminal_events = [
        event
        async for event in events.stream("run-1", after_sequence=None)
        if event.type == "run.terminal"
    ]
    assert len(terminal_events) == 1
    await runner.shutdown()


def pricing() -> ModelPricing:
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
            "input_price": "0.10",
            "output_price": "0.20",
            "pricing_basis": "test",
            "source": "https://example.test/pricing",
        }
    )


class NeverReturningModel:
    model = "fixture-agent"

    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredModelResult[T]:
        del request, output_type
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_timeout_during_model_call_preserves_reserved_cost_counts_and_safe_trace() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    timeout_factory = ControlledTimeoutFactory()

    def make_context(_run_id: str) -> AgentContext:
        budget = BudgetLedger(
            limits=BudgetLimits.from_settings(settings()),
            pricing=pricing(),
            monotonic=clock.monotonic,
        )
        recorder = InMemoryTraceRecorder()
        return AgentContext(
            model_invoker=StructuredModelInvoker(NeverReturningModel(), budget, recorder, clock),
            tools=cast(Any, object()),
            budget=budget,
            events=cast(Any, object()),
            trace_recorder=recorder,
            clock=clock,
        )

    async def invoke_model(*, run_id: str, query: str, context: AgentContext) -> AgentRunResult:
        del run_id, query
        request = StructuredModelRequest(
            purpose="behavior",
            system_prompt="safe system prompt",
            user_payload={"query": SENTINEL},
            output_schema_name="FinalAnswer",
            output_schema_summary={"title": "FinalAnswer", "type": "object"},
            max_output_tokens=100,
        )
        await context.model_invoker.invoke(request, FinalAnswer)
        raise AssertionError("unreachable")

    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=make_context,
        agent_executor=invoke_model,
        timeout_factory=timeout_factory,
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")
    await wait_until(lambda: len(timeout_factory.contexts) == 1)
    await timeout_factory.contexts[0].entered.wait()
    await wait_until(lambda: timeout_factory.contexts[0]._task is not None)
    await asyncio.sleep(0)
    timeout_factory.contexts[0].trigger()
    record = await runner.wait("run-1")

    assert record.final_status is FinalStatus.EXECUTION_FAILED
    assert record.stop_reason is StopReason.TASK_TIMEOUT
    assert record.governance.llm_calls == 1
    assert record.governance.reserved_cost_cny == Decimal("0")
    assert record.governance.committed_cost_cny > 0
    assert len(record.safe_trace.model_calls) == 1
    assert record.safe_trace.model_calls[0].outcome == "cancelled"
    serialized = record.model_dump_json()
    assert SENTINEL not in serialized
    assert "prompt" not in serialized
    assert "raw" not in serialized
    await runner.shutdown()


class SideEffectFailureEventStore(InMemoryEventStore):
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        method: str,
        timing: str,
        persistent: bool = False,
    ) -> None:
        super().__init__(clock=clock)
        self.method = method
        self.timing = timing
        self.persistent = persistent
        self.failures = 0

    def _fails(self, method: str, timing: str) -> bool:
        if self.method != method or self.timing != timing:
            return False
        if self.failures and not self.persistent:
            return False
        self.failures += 1
        return True

    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        if self._fails("create_run", "before"):
            raise RuntimeError(SENTINEL)
        await super().create_run(run_id, owner_token=owner_token)
        if self._fails("create_run", "after"):
            raise RuntimeError(SENTINEL)

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        method = event_type
        if self._fails(method, "before"):
            raise RuntimeError(SENTINEL)
        event = await super().emit(run_id, node, event_type, data, owner_token=owner_token)
        if self._fails(method, "after"):
            raise RuntimeError(SENTINEL)
        return event

    async def has_terminal(self, run_id: str, *, owner_token: object | None = None) -> bool:
        if self._fails("has_terminal", "before"):
            raise RuntimeError(SENTINEL)
        result = await super().has_terminal(run_id, owner_token=owner_token)
        if self._fails("has_terminal", "after"):
            raise RuntimeError(SENTINEL)
        return result

    async def emit_terminal(
        self,
        run_id: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self._fails("emit_terminal", "before"):
            raise RuntimeError(SENTINEL)
        event = await super().emit_terminal(run_id, data, owner_token=owner_token)
        if self._fails("emit_terminal", "after"):
            raise RuntimeError(SENTINEL)
        return event

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        if self._fails("delete_run", "before"):
            raise RuntimeError(SENTINEL)
        result = await super().delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )
        if self._fails("delete_run", "after"):
            raise RuntimeError(SENTINEL)
        return result


class SideEffectFailureRunStore(InMemoryRunStore):
    def __init__(
        self,
        *,
        clock: FakeClock,
        timing: str,
        method: str = "complete",
        persistent: bool = False,
        max_runs: int = 4,
        retention_seconds: int = 3600,
    ) -> None:
        super().__init__(
            max_runs=max_runs,
            retention_seconds=retention_seconds,
            clock=clock,
        )
        self.timing = timing
        self.method = method
        self.persistent = persistent
        self.failures = 0

    def _fails(self, method: str, timing: str) -> bool:
        if self.method != method or self.timing != timing:
            return False
        if self.failures and not self.persistent:
            return False
        self.failures += 1
        return True

    async def complete(
        self,
        run_id: str,
        result: AgentRunResult,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self._fails("complete", "before"):
            raise RuntimeError(SENTINEL)
        record = await super().complete(run_id, result, owner_token=owner_token)
        if self._fails("complete", "after"):
            raise RuntimeError(SENTINEL)
        return record

    async def delete_queued(self, run_id: str, *, owner_token: object | None = None) -> bool:
        if self._fails("delete_queued", "before"):
            raise RuntimeError(SENTINEL)
        deleted = await super().delete_queued(run_id, owner_token=owner_token)
        if self._fails("delete_queued", "after"):
            raise RuntimeError(SENTINEL)
        return deleted

    async def delete_terminal(self, run_id: str, *, owner_token: object | None = None) -> bool:
        if self._fails("delete_terminal", "before"):
            raise RuntimeError(SENTINEL)
        deleted = await super().delete_terminal(run_id, owner_token=owner_token)
        if self._fails("delete_terminal", "after"):
            raise RuntimeError(SENTINEL)
        return deleted


class StartFailureRunStore(InMemoryRunStore):
    def __init__(self, *, clock: FakeClock, timing: str) -> None:
        super().__init__(max_runs=2, retention_seconds=3600, clock=clock)
        self.timing = timing
        self.failed = False

    async def mark_running(self, run_id: str, *, owner_token: object | None = None) -> Any:
        if self.timing == "before" and not self.failed:
            self.failed = True
            raise RuntimeError(SENTINEL)
        record = await super().mark_running(run_id, owner_token=owner_token)
        if self.timing == "after" and not self.failed:
            self.failed = True
            raise RuntimeError(SENTINEL)
        return record


def immediate_executor(
    result_factory: Callable[[str], object],
) -> Callable[..., Coroutine[Any, Any, object]]:
    async def execute(*, run_id: str, query: str, context: AgentContext) -> object:
        del query, context
        return result_factory(run_id)

    return execute


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_run_started_failure_still_writes_one_internal_terminal(timing: str) -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = SideEffectFailureEventStore(clock=clock.now, method="run.started", timing=timing)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    await runner.submit("query")
    record = await runner.wait("run-1")

    assert record.final_status is FinalStatus.INTERNAL_ERROR
    assert record.stop_reason is StopReason.INTERNAL_ERROR
    terminal = [
        event
        async for event in events.stream("run-1", after_sequence=None)
        if event.type == "run.terminal"
    ]
    assert len(terminal) == 1
    await runner.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_mark_running_one_shot_failure_is_reconciled(timing: str) -> None:
    clock = FakeClock()
    runs = StartFailureRunStore(clock=clock, timing=timing)
    events = InMemoryEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    await runner.submit("query")
    assert (await runner.wait("run-1")).final_status is FinalStatus.COMPLETED
    assert await events.has_terminal("run-1") is True
    await runner.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_result", [object(), completed_result("wrong-run")])
async def test_invalid_executor_result_becomes_internal_terminal(bad_result: object) -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(lambda _run_id: bad_result)),
        id_factory=lambda: "run-1",
    )

    await runner.submit("query")
    record = await runner.wait("run-1")

    assert record.final_status is FinalStatus.INTERNAL_ERROR
    assert await events.has_terminal("run-1") is True
    await runner.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_complete_one_shot_side_effect_failure_is_reconciled(timing: str) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(clock=clock, timing=timing)
    events = InMemoryEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    await runner.submit("query")
    record = await runner.wait("run-1")

    assert record.final_status is FinalStatus.COMPLETED
    assert await events.has_terminal("run-1") is True
    await runner.shutdown()


@pytest.mark.asyncio
async def test_persistent_complete_failure_fail_stops_instead_of_returning_running() -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(clock=clock, timing="before", persistent=True)
    events = InMemoryEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.wait("run-1")
    assert (await runs.get("run-1")).lifecycle_status == "running"
    assert await events.has_terminal("run-1") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "timing"),
    [
        ("has_terminal", "before"),
        ("has_terminal", "after"),
        ("emit_terminal", "before"),
        ("emit_terminal", "after"),
    ],
)
async def test_terminal_event_one_shot_failures_are_reconciled(method: str, timing: str) -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = SideEffectFailureEventStore(clock=clock.now, method=method, timing=timing)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    await runner.submit("query")
    record = await runner.wait("run-1")

    assert record.final_status is FinalStatus.COMPLETED
    terminal = [
        event
        async for event in events.stream("run-1", after_sequence=None)
        if event.type == "run.terminal"
    ]
    assert len(terminal) == 1
    await runner.shutdown()


@pytest.mark.asyncio
async def test_persistent_terminal_failure_fail_stops_runner_and_wait() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=3, retention_seconds=3600, clock=clock)
    events = SideEffectFailureEventStore(
        clock=clock.now,
        method="emit_terminal",
        timing="before",
        persistent=True,
    )
    ids = iter(("run-1", "run-2"))
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )
    await runner.submit("query")

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.wait("run-1")
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("second")


@pytest.mark.asyncio
async def test_submit_does_not_delete_preexisting_event_run_on_create_collision() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    await events.create_run("collision")
    await events.emit("collision", "runtime", "run.created", {"status": "queued"})
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "collision",
    )

    with pytest.raises(RunSubmissionFailed, match="run submission failed"):
        await runner.submit("query")
    with pytest.raises(RunNotFound):
        await runs.get("collision")
    assert await events.high_water_mark("collision") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_submit_create_run_failures_respect_event_ownership(timing: str) -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = SideEffectFailureEventStore(clock=clock.now, method="create_run", timing=timing)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    with pytest.raises(RunSubmissionFailed, match="run submission failed"):
        await runner.submit("query")
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")


@pytest.mark.asyncio
async def test_submit_create_task_failure_closes_coroutine_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = InMemoryEventStore(clock=clock.now)

    def fail_create_task(coroutine: Coroutine[Any, Any, None], **kwargs: object) -> None:
        del coroutine, kwargs
        raise RuntimeError(SENTINEL)

    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    monkeypatch.setattr(asyncio, "create_task", fail_create_task)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RunSubmissionFailed, match="run submission failed"):
            await runner.submit("query")
        gc.collect()
    assert not any("never awaited" in str(item.message) for item in caught)
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")


@pytest.mark.asyncio
async def test_rollback_delete_before_side_effect_failure_preserves_pair_and_fail_stops() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = SideEffectFailureEventStore(
        clock=clock.now,
        method="delete_run",
        timing="before",
        persistent=True,
    )
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    original_emit = events.emit

    async def fail_created(
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if event_type == "run.created":
            raise RuntimeError(SENTINEL)
        return await original_emit(run_id, node, event_type, data, owner_token=owner_token)

    events.emit = fail_created  # type: ignore[method-assign]

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("query")
    assert (await runs.get("run-1")).lifecycle_status == "queued"
    assert await events.high_water_mark("run-1") == 0


@pytest.mark.asyncio
async def test_rollback_delete_after_side_effect_failure_confirms_and_deletes_run() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = SideEffectFailureEventStore(clock=clock.now, method="delete_run", timing="after")
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    original_emit = events.emit

    async def fail_created(
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if event_type == "run.created":
            raise RuntimeError(SENTINEL)
        return await original_emit(run_id, node, event_type, data, owner_token=owner_token)

    events.emit = fail_created  # type: ignore[method-assign]

    with pytest.raises(RunSubmissionFailed, match="run submission failed"):
        await runner.submit("query")
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")


@pytest.mark.asyncio
async def test_prune_delete_after_side_effect_failure_removes_both_stores() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=1, clock=clock)
    events = SideEffectFailureEventStore(clock=clock.now, method="delete_run", timing="after")
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "new",
    )

    await runner.submit("query")
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")
    await runner.shutdown()


@pytest.mark.asyncio
async def test_prune_delete_before_side_effect_failure_retains_both_stores() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=1, clock=clock)
    events = SideEffectFailureEventStore(
        clock=clock.now,
        method="delete_run",
        timing="before",
        persistent=True,
    )
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "new",
    )

    await runner.submit("query")
    assert (await runs.get("old")).lifecycle_status == "terminal"
    assert await events.high_water_mark("old") == 3
    await runner.shutdown()


@pytest.mark.asyncio
async def test_expired_terminal_with_active_stream_still_consumes_physical_capacity() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=1, retention_seconds=1, clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    stream = events.stream("old", after_sequence=None)
    await anext(stream)
    ids = iter(("blocked", "accepted"))
    runner = AnalysisRunner(
        settings=settings(max_runs=1, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    with pytest.raises(RunCapacityExceeded):
        await runner.submit("blocked")
    assert (await runs.get("old")).lifecycle_status == "terminal"
    assert await events.high_water_mark("old") == 3
    await stream.aclose()
    created = await runner.submit("accepted")
    assert created.run_id == "accepted"
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")
    await runner.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_rollback_reconciles_one_shot_run_delete_failure(timing: str) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_queued",
        timing=timing,
    )
    events = SideEffectFailureEventStore(
        clock=clock.now,
        method="run.created",
        timing="before",
    )
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    with pytest.raises(RunSubmissionFailed, match="run submission failed"):
        await runner.submit("query")
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")


@pytest.mark.asyncio
async def test_persistent_rollback_run_delete_restores_pair_and_fail_stops() -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_queued",
        timing="before",
        persistent=True,
    )
    events = SideEffectFailureEventStore(
        clock=clock.now,
        method="run.created",
        timing="before",
    )
    ids = iter(("run-1", "run-2"))
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("query")
    assert (await runs.get("run-1")).lifecycle_status == "queued"
    assert await events.high_water_mark("run-1") == 1
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_prune_reconciles_one_shot_terminal_run_delete_failure(timing: str) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_terminal",
        timing=timing,
        max_runs=2,
        retention_seconds=1,
    )
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "new",
    )

    assert (await runner.submit("query")).run_id == "new"
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")
    await runner.shutdown()


@pytest.mark.asyncio
async def test_persistent_prune_run_delete_fail_stops_after_event_delete() -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_terminal",
        timing="before",
        persistent=True,
        max_runs=2,
        retention_seconds=1,
    )
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    ids = iter(("new", "rejected"))
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("query")
    assert (await runs.get("old")).lifecycle_status == "terminal"
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class DeleteThenBlockEventStore(InMemoryEventStore):
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        fail_created: bool,
    ) -> None:
        super().__init__(clock=clock)
        self.fail_created = fail_created
        self.deleted = asyncio.Event()

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self.fail_created and event_type == "run.created":
            self.fail_created = False
            raise RuntimeError(SENTINEL)
        return await super().emit(run_id, node, event_type, data, owner_token=owner_token)

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        deleted = await super().delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )
        self.deleted.set()
        await asyncio.Event().wait()
        return deleted


class DeleteThenBlockRunStore(InMemoryRunStore):
    def __init__(
        self,
        *,
        clock: FakeClock,
        method: str,
        retention_seconds: int = 3600,
    ) -> None:
        super().__init__(max_runs=2, retention_seconds=retention_seconds, clock=clock)
        self.method = method
        self.deleted = asyncio.Event()

    async def delete_queued(self, run_id: str, *, owner_token: object | None = None) -> bool:
        deleted = await super().delete_queued(run_id, owner_token=owner_token)
        if self.method == "delete_queued":
            self.deleted.set()
            await asyncio.Event().wait()
        return deleted

    async def delete_terminal(self, run_id: str, *, owner_token: object | None = None) -> bool:
        deleted = await super().delete_terminal(run_id, owner_token=owner_token)
        if self.method == "delete_terminal":
            self.deleted.set()
            await asyncio.Event().wait()
        return deleted


@pytest.mark.asyncio
async def test_rollback_delays_cancellation_until_both_owned_records_are_deleted() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = DeleteThenBlockEventStore(clock=clock.now, fail_created=True)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.deleted.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")


@pytest.mark.asyncio
async def test_prune_delays_cancellation_until_terminal_pair_is_deleted() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=1, clock=clock)
    events = DeleteThenBlockEventStore(clock=clock.now, fail_created=False)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "new",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.deleted.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")


@pytest.mark.asyncio
async def test_rollback_reconciles_run_delete_before_rethrowing_cancellation() -> None:
    clock = FakeClock()
    runs = DeleteThenBlockRunStore(clock=clock, method="delete_queued")
    events = SideEffectFailureEventStore(
        clock=clock.now,
        method="run.created",
        timing="before",
    )
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await runs.deleted.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")


@pytest.mark.asyncio
async def test_prune_reconciles_run_delete_before_rethrowing_cancellation() -> None:
    clock = FakeClock()
    runs = DeleteThenBlockRunStore(
        clock=clock,
        method="delete_terminal",
        retention_seconds=1,
    )
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "new",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await runs.deleted.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")


class RacingFailStopEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.racing_created = asyncio.Event()
        self.release_racing = asyncio.Event()

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        event = await super().emit(run_id, node, event_type, data, owner_token=owner_token)
        if run_id == "racing" and event_type == "run.created":
            self.racing_created.set()
            await self.release_racing.wait()
        return event

    async def emit_terminal(
        self,
        run_id: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if run_id == "failing":
            raise RuntimeError(SENTINEL)
        return await super().emit_terminal(run_id, data, owner_token=owner_token)


@pytest.mark.asyncio
async def test_inflight_submit_rechecks_fail_stop_before_starting_task() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=3, retention_seconds=3600, clock=clock)
    events = RacingFailStopEventStore(clock=clock.now)
    release_failing = asyncio.Event()
    executor_calls: list[str] = []

    async def execute(*, run_id: str, query: str, context: AgentContext) -> AgentRunResult:
        del query, context
        executor_calls.append(run_id)
        if run_id == "failing":
            await release_failing.wait()
        return completed_result(run_id)

    ids = iter(("failing", "racing"))
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=execute,
        id_factory=lambda: next(ids),
    )
    await runner.submit("first")
    await wait_until(lambda: executor_calls == ["failing"])
    racing = asyncio.create_task(runner.submit("second"))
    await events.racing_created.wait()
    release_failing.set()
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.wait("failing")
    events.release_racing.set()

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await racing
    assert executor_calls == ["failing"]
    with pytest.raises(RunNotFound):
        await runs.get("racing")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("racing")


class FutureTerminalEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.clock = clock
        self.replay: list[RunEvent] = []

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> RunEvent:
        event = await super().emit(run_id, node, event_type, data, owner_token=owner_token)
        self.replay.append(event)
        return event

    async def has_terminal(self, run_id: str, *, owner_token: object | None = None) -> bool:
        del run_id, owner_token
        return True

    async def stream(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
    ) -> AsyncGenerator[RunEvent, None]:
        del after_sequence
        for event in tuple(self.replay):
            yield event
        yield RunEvent(
            event_id=f"{run_id}:3",
            sequence=3,
            run_id=run_id,
            timestamp=self.clock(),
            node="runtime",
            type="run.terminal",
            data={"final_status": "completed", "stop_reason": "answer_complete"},
        )


@pytest.mark.asyncio
async def test_terminal_matcher_ignores_events_beyond_high_water_snapshot() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = FutureTerminalEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.wait("run-1")


class CreateThenFailBlockingReadRunStore(InMemoryRunStore):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(max_runs=3, retention_seconds=3600, clock=clock)
        self.block_get = False
        self.read_started = asyncio.Event()

    async def create(
        self,
        run_id: str,
        query: str,
        *,
        owner_token: object | None = None,
    ) -> Any:
        record = await super().create(run_id, query, owner_token=owner_token)
        if run_id == "run-1":
            self.block_get = True
            raise RuntimeError(SENTINEL)
        return record

    async def get(self, run_id: str, *, owner_token: object | None = None) -> Any:
        if run_id == "run-1" and self.block_get:
            self.block_get = False
            self.read_started.set()
            await asyncio.Event().wait()
        return await super().get(run_id, owner_token=owner_token)


@pytest.mark.asyncio
async def test_run_create_reconciliation_delays_cancellation_through_blocked_read() -> None:
    clock = FakeClock()
    runs = CreateThenFailBlockingReadRunStore(clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    ids = iter(("run-1", "run-2"))
    runner = AnalysisRunner(
        settings=settings(max_runs=3),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    submission = asyncio.create_task(runner.submit("first"))
    await runs.read_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    assert (await runner.submit("second")).run_id == "run-2"
    assert (await runner.wait("run-2")).final_status is FinalStatus.COMPLETED


class EventCreateThenFailBlockingReadStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.block_read = False
        self.read_started = asyncio.Event()

    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        await super().create_run(run_id, owner_token=owner_token)
        if run_id == "run-1":
            self.block_read = True
            raise RuntimeError(SENTINEL)

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        if run_id == "run-1" and self.block_read:
            self.block_read = False
            self.read_started.set()
            await asyncio.Event().wait()
        return await super().high_water_mark(run_id, owner_token=owner_token)


@pytest.mark.asyncio
async def test_event_create_reconciliation_delays_cancellation_through_blocked_read() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=3, retention_seconds=3600, clock=clock)
    events = EventCreateThenFailBlockingReadStore(clock=clock.now)
    ids = iter(("run-1", "run-2"))
    runner = AnalysisRunner(
        settings=settings(max_runs=3),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    submission = asyncio.create_task(runner.submit("first"))
    await events.read_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")
    assert (await runner.submit("second")).run_id == "run-2"
    assert (await runner.wait("run-2")).final_status is FinalStatus.COMPLETED


class PersistentRunCreateReadFailureStore(InMemoryRunStore):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.created = False

    async def create(
        self,
        run_id: str,
        query: str,
        *,
        owner_token: object | None = None,
    ) -> Any:
        await super().create(run_id, query, owner_token=owner_token)
        self.created = True
        raise RuntimeError(SENTINEL)

    async def get(self, run_id: str, *, owner_token: object | None = None) -> Any:
        if self.created:
            raise RuntimeError(SENTINEL)
        return await super().get(run_id, owner_token=owner_token)


@pytest.mark.asyncio
async def test_persistent_run_create_readback_failure_stably_fail_stops() -> None:
    clock = FakeClock()
    runs = PersistentRunCreateReadFailureStore(
        max_runs=2,
        retention_seconds=3600,
        clock=clock,
    )
    events = InMemoryEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("query")
    assert (await InMemoryRunStore.get(runs, "run-1")).lifecycle_status == "queued"
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class PersistentEventCreateReadFailureStore(InMemoryEventStore):
    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        await super().create_run(run_id, owner_token=owner_token)
        raise RuntimeError(SENTINEL)

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        try:
            await super().high_water_mark(run_id, owner_token=owner_token)
        except EventRunNotFound:
            raise
        raise RuntimeError(SENTINEL)


@pytest.mark.asyncio
async def test_persistent_event_create_readback_failure_stably_fail_stops() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = PersistentEventCreateReadFailureStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("query")
    assert (await runs.get("run-1")).lifecycle_status == "queued"
    assert await InMemoryEventStore.high_water_mark(events, "run-1") == 0
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class DeleteThenFailBlockingReadEventStore(InMemoryEventStore):
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        fail_created: bool,
    ) -> None:
        super().__init__(clock=clock)
        self.fail_created = fail_created
        self.block_read = False
        self.read_started = asyncio.Event()

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self.fail_created and event_type == "run.created":
            self.fail_created = False
            raise RuntimeError(SENTINEL)
        return await super().emit(run_id, node, event_type, data, owner_token=owner_token)

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        await super().delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )
        self.block_read = True
        raise RuntimeError(SENTINEL)

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        if self.block_read:
            self.block_read = False
            self.read_started.set()
            await asyncio.Event().wait()
        return await super().high_water_mark(run_id, owner_token=owner_token)


@pytest.mark.asyncio
async def test_rollback_delete_readback_delays_cancellation_until_pair_is_clean() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = DeleteThenFailBlockingReadEventStore(clock=clock.now, fail_created=True)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.read_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")
    assert (await runner.submit("replacement")).run_id == "run-1"
    assert (await runner.wait("run-1")).final_status is FinalStatus.COMPLETED


@pytest.mark.asyncio
async def test_prune_delete_readback_delays_cancellation_until_pair_is_clean() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=1, clock=clock)
    events = DeleteThenFailBlockingReadEventStore(clock=clock.now, fail_created=False)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "new",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.read_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    with pytest.raises(RunNotFound):
        await runs.get("old")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("old")
    assert (await runner.submit("replacement")).run_id == "new"
    assert (await runner.wait("new")).final_status is FinalStatus.COMPLETED


class RestoreCancellationEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime], window: str) -> None:
        super().__init__(clock=clock)
        self.window = window
        self.initial_created_failed = False
        self.restore_create_calls = 0
        self.restore_emit_calls = 0
        self.restore_started = asyncio.Event()

    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        if self.initial_created_failed:
            self.restore_create_calls += 1
            if self.window == "create" and self.restore_create_calls == 1:
                self.restore_started.set()
                await asyncio.Event().wait()
        await super().create_run(run_id, owner_token=owner_token)

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if event_type == "run.created" and not self.initial_created_failed:
            self.initial_created_failed = True
            raise RuntimeError(SENTINEL)
        if event_type == "run.created":
            self.restore_emit_calls += 1
            if self.window == "emit" and self.restore_emit_calls == 1:
                self.restore_started.set()
                await asyncio.Event().wait()
        return await super().emit(run_id, node, event_type, data, owner_token=owner_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["create", "emit"])
async def test_restore_compensation_delays_cancellation_until_run_created(window: str) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_queued",
        timing="before",
        persistent=True,
    )
    events = RestoreCancellationEventStore(clock=clock.now, window=window)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.restore_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert (await runs.get("run-1")).lifecycle_status == "queued"
    assert await events.high_water_mark("run-1") == 1
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class IncompleteSnapshotEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.replay: list[RunEvent] = []
        self.stream_called = False

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> RunEvent:
        event = await super().emit(run_id, node, event_type, data, owner_token=owner_token)
        self.replay.append(event)
        return event

    async def has_terminal(self, run_id: str, *, owner_token: object | None = None) -> bool:
        del run_id, owner_token
        return True

    async def replay_snapshot(
        self,
        run_id: str,
        *,
        high_water_mark: int,
        owner_token: object | None = None,
    ) -> tuple[RunEvent, ...]:
        del run_id, high_water_mark, owner_token
        return tuple(self.replay[:1])

    async def stream(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
    ) -> AsyncGenerator[RunEvent, None]:
        del run_id, after_sequence
        self.stream_called = True
        raise RuntimeError(SENTINEL)
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_incomplete_terminal_snapshot_fail_stops_without_live_stream() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = IncompleteSnapshotEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.wait("run-1")
    await runner.shutdown()
    assert events.stream_called is False


class NonBoundaryTerminalSnapshotEventStore(IncompleteSnapshotEventStore):
    async def stream(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
    ) -> AsyncGenerator[RunEvent, None]:
        del after_sequence
        for event in await self.replay_snapshot(run_id, high_water_mark=3):
            yield event

    async def replay_snapshot(
        self,
        run_id: str,
        *,
        high_water_mark: int,
        owner_token: object | None = None,
    ) -> tuple[RunEvent, ...]:
        del high_water_mark, owner_token
        terminal = RunEvent(
            event_id=f"{run_id}:2",
            sequence=2,
            run_id=run_id,
            timestamp=self._clock(),
            node="runtime",
            type="run.terminal",
            data={"final_status": "completed", "stop_reason": "answer_complete"},
        )
        trailing = self.replay[1].model_copy(update={"event_id": f"{run_id}:3", "sequence": 3})
        return self.replay[0], terminal, trailing

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        del run_id, owner_token
        return 3


@pytest.mark.asyncio
async def test_matching_terminal_must_be_snapshot_boundary_event() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = NonBoundaryTerminalSnapshotEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )
    await runner.submit("query")

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.wait("run-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting_nonempty", [False, True])
async def test_failed_rollback_never_restores_an_unowned_event_run(
    preexisting_nonempty: bool,
) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_queued",
        timing="before",
        persistent=True,
    )
    events = InMemoryEventStore(clock=clock.now)
    await events.create_run("collision")
    if preexisting_nonempty:
        await events.emit(
            "collision",
            "runtime",
            "run.created",
            {"status": "queued"},
        )
    before_high_water = await events.high_water_mark("collision")
    before_events = await events.replay_snapshot(
        "collision",
        high_water_mark=before_high_water,
    )
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "collision",
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("query")

    assert (await runs.get("collision")).lifecycle_status == "queued"
    assert await events.high_water_mark("collision") == before_high_water
    assert (
        await events.replay_snapshot(
            "collision",
            high_water_mark=before_high_water,
        )
        == before_events
    )
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class MaskCancellationWithCleanupError:
    def __init__(self) -> None:
        self.boundary_started = asyncio.Event()
        self.cleanup_task: asyncio.Task[None] | None = None
        self.masked = False

    async def mask_once(self) -> None:
        if self.masked:
            return
        self.masked = True
        self.boundary_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleanup_task = asyncio.create_task(self._fail_cleanup())
            await self.cleanup_task

    @staticmethod
    async def _fail_cleanup() -> None:
        await asyncio.sleep(0)
        raise RuntimeError(SENTINEL)


class MaskedRollbackDeleteEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.masker = MaskCancellationWithCleanupError()
        self.fail_initial_emit = True

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self.fail_initial_emit and event_type == "run.created":
            self.fail_initial_emit = False
            raise RuntimeError(SENTINEL)
        return await super().emit(run_id, node, event_type, data, owner_token=owner_token)

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        deleted = await super().delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )
        await self.masker.mask_once()
        return deleted


@pytest.mark.asyncio
async def test_cleanup_exception_cannot_mask_cancellation_during_rollback_delete() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=3, retention_seconds=3600, clock=clock)
    events = MaskedRollbackDeleteEventStore(clock=clock.now)
    ids = iter(("run-1", "run-2"))
    runner = AnalysisRunner(
        settings=settings(max_runs=3),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    submission = asyncio.create_task(runner.submit("first"))
    await events.masker.boundary_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert submission.cancelled()
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")
    assert events.masker.cleanup_task is not None
    assert events.masker.cleanup_task.done()
    assert (await runner.submit("second")).run_id == "run-2"
    assert (await runner.wait("run-2")).final_status is FinalStatus.COMPLETED


class MaskedRunReadStore(InMemoryRunStore):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(max_runs=3, retention_seconds=3600, clock=clock)
        self.masker = MaskCancellationWithCleanupError()
        self.failed_create = False

    async def create(
        self,
        run_id: str,
        query: str,
        *,
        owner_token: object | None = None,
    ) -> Any:
        record = await super().create(run_id, query, owner_token=owner_token)
        if run_id == "run-1":
            self.failed_create = True
            raise RuntimeError(SENTINEL)
        return record

    async def get(self, run_id: str, *, owner_token: object | None = None) -> Any:
        if run_id == "run-1" and self.failed_create:
            await self.masker.mask_once()
        return await super().get(run_id, owner_token=owner_token)


class MaskedEventHighWaterStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.masker = MaskCancellationWithCleanupError()
        self.failed_create = False

    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        await super().create_run(run_id, owner_token=owner_token)
        if run_id == "run-1":
            self.failed_create = True
            raise RuntimeError(SENTINEL)

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        if run_id == "run-1" and self.failed_create:
            await self.masker.mask_once()
        return await super().high_water_mark(run_id, owner_token=owner_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["run_get", "event_high_water"])
async def test_readback_cleanup_exception_preserves_cancellation(boundary: str) -> None:
    clock = FakeClock()
    if boundary == "run_get":
        masked_runs = MaskedRunReadStore(clock=clock)
        runs: InMemoryRunStore = masked_runs
        events: InMemoryEventStore = InMemoryEventStore(clock=clock.now)
        masker = masked_runs.masker
    else:
        runs = InMemoryRunStore(max_runs=3, retention_seconds=3600, clock=clock)
        masked_events = MaskedEventHighWaterStore(clock=clock.now)
        events = masked_events
        masker = masked_events.masker
    ids = iter(("run-1", "run-2"))
    runner = AnalysisRunner(
        settings=settings(max_runs=3),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: next(ids),
    )

    submission = asyncio.create_task(runner.submit("first"))
    await masker.boundary_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert submission.cancelled()
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await events.high_water_mark("run-1")
    assert masker.cleanup_task is not None
    assert masker.cleanup_task.done()
    assert (await runner.submit("second")).run_id == "run-2"
    assert (await runner.wait("run-2")).final_status is FinalStatus.COMPLETED


class MaskedRestoreEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime], boundary: str) -> None:
        super().__init__(clock=clock)
        self.boundary = boundary
        self.masker = MaskCancellationWithCleanupError()
        self.initial_emit_failed = False

    async def create_run(self, run_id: str, *, owner_token: object | None = None) -> None:
        if self.initial_emit_failed and self.boundary == "restore_create":
            await self.masker.mask_once()
        await super().create_run(run_id, owner_token=owner_token)

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if event_type == "run.created" and not self.initial_emit_failed:
            self.initial_emit_failed = True
            raise RuntimeError(SENTINEL)
        if event_type == "run.created" and self.boundary == "restore_emit":
            await self.masker.mask_once()
        return await super().emit(run_id, node, event_type, data, owner_token=owner_token)

    async def replay_snapshot(
        self,
        run_id: str,
        *,
        high_water_mark: int,
        owner_token: object | None = None,
    ) -> tuple[RunEvent, ...]:
        if self.initial_emit_failed and self.boundary == "restore_replay":
            await self.masker.mask_once()
        return await super().replay_snapshot(
            run_id,
            high_water_mark=high_water_mark,
            owner_token=owner_token,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["restore_create", "restore_emit", "restore_replay"],
)
async def test_restore_cleanup_exception_preserves_cancellation(boundary: str) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_queued",
        timing="before",
        persistent=True,
    )
    events = MaskedRestoreEventStore(clock=clock.now, boundary=boundary)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.masker.boundary_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert submission.cancelled()
    assert (await runs.get("run-1")).lifecycle_status == "queued"
    assert await events.high_water_mark("run-1") == 1
    restored = await events.replay_snapshot("run-1", high_water_mark=1)
    assert len(restored) == 1
    assert restored[0].type == "run.created"
    assert dict(restored[0].data) == {"status": "queued"}
    assert events.masker.cleanup_task is not None
    assert events.masker.cleanup_task.done()
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class ForeignGenerationOnFailedRunCreate(InMemoryRunStore):
    async def create(
        self,
        run_id: str,
        query: str,
        *,
        owner_token: object | None = None,
    ) -> Any:
        del query, owner_token
        await InMemoryRunStore.create(self, run_id, "foreign query")
        raise RuntimeError(SENTINEL)


@pytest.mark.asyncio
async def test_run_create_failure_never_deletes_a_foreign_generation() -> None:
    clock = FakeClock()
    runs = ForeignGenerationOnFailedRunCreate(
        max_runs=2,
        retention_seconds=3600,
        clock=clock,
    )
    events = InMemoryEventStore(clock=clock.now)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "collision",
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("our query")

    foreign = await InMemoryRunStore.get(runs, "collision")
    assert foreign.query == "foreign query"
    assert foreign.lifecycle_status == "queued"
    assert executor.calls == []
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class ForeignGenerationOnFailedEventCreate(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime], nonempty: bool) -> None:
        super().__init__(clock=clock)
        self.nonempty = nonempty

    async def create_run(
        self,
        run_id: str,
        *,
        owner_token: object | None = None,
    ) -> None:
        del owner_token
        await InMemoryEventStore.create_run(self, run_id)
        if self.nonempty:
            await InMemoryEventStore.emit(
                self,
                run_id,
                "runtime",
                "run.created",
                {"status": "queued"},
            )
        raise RuntimeError(SENTINEL)


@pytest.mark.asyncio
@pytest.mark.parametrize("nonempty", [False, True])
async def test_event_create_failure_never_deletes_a_foreign_generation(
    nonempty: bool,
) -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = ForeignGenerationOnFailedEventCreate(clock=clock.now, nonempty=nonempty)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "collision",
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("our query")

    assert await InMemoryEventStore.high_water_mark(events, "collision") == int(nonempty)
    if nonempty:
        snapshot = await InMemoryEventStore.replay_snapshot(
            events,
            "collision",
            high_water_mark=1,
        )
        assert len(snapshot) == 1
        assert snapshot[0].type == "run.created"
        assert dict(snapshot[0].data) == {"status": "queued"}
    with pytest.raises(RunNotFound):
        await runs.get("collision")
    assert executor.calls == []
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class ForeignReplacementDuringRestoreEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.initial_emit = True

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self.initial_emit and event_type == "run.created":
            self.initial_emit = False
            raise RuntimeError(SENTINEL)
        return await super().emit(
            run_id,
            node,
            event_type,
            data,
            owner_token=owner_token,
        )

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        return await super().delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )


class ForeignReplacementBeforeRestoreRunStore(SideEffectFailureRunStore):
    def __init__(
        self,
        *,
        clock: FakeClock,
        events: InMemoryEventStore,
    ) -> None:
        super().__init__(
            clock=clock,
            method="delete_queued",
            timing="before",
            persistent=True,
        )
        self.events = events
        self.replaced = False

    async def delete_queued(
        self,
        run_id: str,
        *,
        owner_token: object | None = None,
    ) -> bool:
        if not self.replaced:
            self.replaced = True
            await InMemoryEventStore.create_run(self.events, run_id)
        raise RuntimeError(SENTINEL)


@pytest.mark.asyncio
async def test_restore_never_emits_into_a_foreign_replacement_generation() -> None:
    clock = FakeClock()
    events = ForeignReplacementDuringRestoreEventStore(clock=clock.now)
    runs = ForeignReplacementBeforeRestoreRunStore(clock=clock, events=events)
    executor = BlockingExecutor()
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=executor,
        id_factory=lambda: "collision",
    )

    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("our query")

    assert await InMemoryEventStore.high_water_mark(events, "collision") == 0
    assert (
        await InMemoryEventStore.replay_snapshot(
            events,
            "collision",
            high_water_mark=0,
        )
        == ()
    )
    assert (await runs.get("collision")).lifecycle_status == "queued"
    assert executor.calls == []
    with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
        await runner.submit("must be rejected")


class SwallowCancellationRollbackEventStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        super().__init__(clock=clock)
        self.initial_emit = True
        self.delete_started = asyncio.Event()

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self.initial_emit and event_type == "run.created":
            self.initial_emit = False
            raise RuntimeError(SENTINEL)
        return await super().emit(
            run_id,
            node,
            event_type,
            data,
            owner_token=owner_token,
        )

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        deleted = await super().delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )
        self.delete_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return deleted
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_dependency_swallowing_cancellation_still_cancels_submit_after_cleanup() -> None:
    clock = FakeClock()
    runs = InMemoryRunStore(max_runs=2, retention_seconds=3600, clock=clock)
    events = SwallowCancellationRollbackEventStore(clock=clock.now)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.delete_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert submission.cancelled()
    with pytest.raises(RunNotFound):
        await runs.get("run-1")
    with pytest.raises(EventRunNotFound):
        await InMemoryEventStore.high_water_mark(events, "run-1")


class SwallowCancellationRestoreReplayStore(InMemoryEventStore):
    def __init__(self, *, clock: Callable[[], datetime], uncancel: bool) -> None:
        super().__init__(clock=clock)
        self.uncancel = uncancel
        self.initial_emit = True
        self.replay_started = asyncio.Event()

    async def emit(
        self,
        run_id: str,
        node: str,
        event_type: str,
        data: Any,
        *,
        owner_token: object | None = None,
    ) -> Any:
        if self.initial_emit and event_type == "run.created":
            self.initial_emit = False
            raise RuntimeError(SENTINEL)
        return await super().emit(
            run_id,
            node,
            event_type,
            data,
            owner_token=owner_token,
        )

    async def replay_snapshot(
        self,
        run_id: str,
        *,
        high_water_mark: int,
        owner_token: object | None = None,
    ) -> tuple[RunEvent, ...]:
        self.replay_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self.uncancel:
                task = asyncio.current_task()
                assert task is not None
                task.uncancel()
        return await super().replay_snapshot(
            run_id,
            high_water_mark=high_water_mark,
            owner_token=owner_token,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("uncancel", [False, True])
async def test_restore_replay_samples_swallowed_cancellation_but_respects_uncancel(
    uncancel: bool,
) -> None:
    clock = FakeClock()
    runs = SideEffectFailureRunStore(
        clock=clock,
        method="delete_queued",
        timing="before",
        persistent=True,
    )
    events = SwallowCancellationRestoreReplayStore(clock=clock.now, uncancel=uncancel)
    runner = AnalysisRunner(
        settings=settings(),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "run-1",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await events.replay_started.wait()
    submission.cancel()

    if uncancel:
        with pytest.raises(RunConsistencyError, match="run stores are inconsistent"):
            await submission
        assert not submission.cancelled()
        assert submission.cancelling() == 0
    else:
        with pytest.raises(asyncio.CancelledError):
            await submission
        assert submission.cancelled()
    assert (await runs.get("run-1")).lifecycle_status == "queued"
    assert await events.high_water_mark("run-1") == 1


class MaskedExpiredIdsRunStore(InMemoryRunStore):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(max_runs=2, retention_seconds=1, clock=clock)
        self.masker = MaskCancellationWithCleanupError()

    async def expired_terminal_ids(self) -> tuple[str, ...]:
        await self.masker.mask_once()
        return await super().expired_terminal_ids()


@pytest.mark.asyncio
async def test_prune_expired_ids_cleanup_exception_cannot_mask_cancellation() -> None:
    clock = FakeClock()
    runs = MaskedExpiredIdsRunStore(clock=clock)
    events = InMemoryEventStore(clock=clock.now)
    await add_terminal_pair(runs, events, "old")
    clock.advance(1)
    runner = AnalysisRunner(
        settings=settings(max_runs=2, run_retention_seconds=1),
        runs=runs,
        events=events,
        context_factory=lambda _run_id: context_for(clock),
        agent_executor=cast(Any, immediate_executor(completed_result)),
        id_factory=lambda: "new",
    )

    submission = asyncio.create_task(runner.submit("query"))
    await runs.masker.boundary_started.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert submission.cancelled()
    assert (await runs.get("old")).lifecycle_status == "terminal"
    assert await events.high_water_mark("old") == 3
    assert runs.masker.cleanup_task is not None
    assert runs.masker.cleanup_task.done()
