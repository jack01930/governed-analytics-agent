"""In-memory run lifecycle storage and bounded background execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from governed_analytics.agent.contracts import (
    AgentRunResult,
    EvidenceItem,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    RunLifecycleStatus,
    SafeTrace,
    StopReason,
)
from governed_analytics.agent.graph import run_agent
from governed_analytics.agent.ports import AgentContext
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.runtime.events import (
    EventStore,
    RunEvent,
)
from governed_analytics.runtime.events import RunNotFound as EventRunNotFound

type TimeoutFactory = Callable[[float], AbstractAsyncContextManager[None]]
type ContextFactory = Callable[[str], AgentContext]
type AgentExecutor = Callable[..., Awaitable[AgentRunResult]]
type RunIdFactory = Callable[[], str]
type WallClock = Callable[[], datetime]


class RunStoreError(RuntimeError):
    """Base class for stable run-store failures."""


class RunNotFound(RunStoreError):
    def __init__(self) -> None:
        super().__init__("run not found")


class RunAlreadyExists(RunStoreError):
    def __init__(self) -> None:
        super().__init__("run already exists")


class RunCapacityExceeded(RunStoreError):
    def __init__(self) -> None:
        super().__init__("run capacity exceeded")


class RunStateConflict(RunStoreError):
    def __init__(self) -> None:
        super().__init__("run lifecycle state conflict")


class RunnerShutdown(RunStoreError):
    def __init__(self) -> None:
        super().__init__("analysis runner is shut down")


class RunSubmissionFailed(RunStoreError):
    def __init__(self) -> None:
        super().__init__("run submission failed")


class RunConsistencyError(RunStoreError):
    def __init__(self) -> None:
        super().__init__("run stores are inconsistent")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class _DelayedCancellation:
    def __init__(self) -> None:
        self.pending: asyncio.CancelledError | None = None

    def observe(self, error: BaseException) -> None:
        if (
            isinstance(error, asyncio.CancelledError)
            and _task_is_cancelling()
            and self.pending is None
        ):
            self.pending = error

    def raise_if_pending(self) -> None:
        if self.pending is not None:
            raise self.pending


class RunRecord(BaseModel):
    """Frozen public projection of one run, excluding internal agent state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    query: str
    lifecycle_status: RunLifecycleStatus
    created_at: datetime
    started_at: datetime | None = None
    terminal_at: datetime | None = None
    final_status: FinalStatus | None = None
    answer: str | None = None
    evidence: tuple[EvidenceItem, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    repair_count: int = Field(default=0, ge=0)
    governance: GovernanceSnapshot = GovernanceSnapshot()
    stop_reason: StopReason | None = None
    safe_trace: SafeTrace = SafeTrace()

    @field_validator("created_at", "started_at", "terminal_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RunRecord:
        terminal_fields = (self.final_status, self.answer, self.stop_reason)
        if self.lifecycle_status is RunLifecycleStatus.QUEUED:
            if self.started_at is not None or self.terminal_at is not None:
                raise ValueError("queued run cannot have execution timestamps")
            if any(value is not None for value in terminal_fields):
                raise ValueError("queued run cannot have terminal output")
        elif self.lifecycle_status is RunLifecycleStatus.RUNNING:
            if self.started_at is None or self.terminal_at is not None:
                raise ValueError("running run requires only a started timestamp")
            if any(value is not None for value in terminal_fields):
                raise ValueError("running run cannot have terminal output")
        elif (
            self.started_at is None
            or self.terminal_at is None
            or any(value is None for value in terminal_fields)
        ):
            raise ValueError("terminal run requires complete terminal output")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("run cannot start before creation")
        if self.terminal_at is not None and (
            self.started_at is None or self.terminal_at < self.started_at
        ):
            raise ValueError("run cannot terminate before starting")
        return self


class RunStore(Protocol):
    async def create(self, run_id: str, query: str) -> RunRecord: ...

    async def get(self, run_id: str) -> RunRecord: ...

    async def mark_running(self, run_id: str) -> RunRecord: ...

    async def complete(self, run_id: str, result: AgentRunResult) -> RunRecord: ...

    async def expired_terminal_ids(self) -> tuple[str, ...]: ...

    async def delete_queued(self, run_id: str) -> bool: ...

    async def delete_terminal(self, run_id: str) -> bool: ...


def _copy_model[T: BaseModel](model: T) -> T:
    """Round-trip a safe contract so records never alias caller-owned object graphs."""

    return type(model).model_validate_json(model.model_dump_json())


def _terminal_projection(result: AgentRunResult) -> dict[str, object]:
    answer = _copy_model(result.final_answer)
    governance = _copy_model(result.governance)
    return {
        "final_status": answer.status,
        "answer": str(answer.answer),
        "evidence": tuple(_copy_model(item) for item in result.evidence if item.verified),
        "limitations": tuple(str(item) for item in answer.limitations),
        "evidence_gaps": tuple(str(item) for item in result.evidence_gaps),
        "repair_count": governance.repair_count,
        "governance": governance,
        "stop_reason": answer.stop_reason,
        "safe_trace": _copy_model(result.safe_trace),
    }


def _terminal_record_matches(record: RunRecord, projection: Mapping[str, object]) -> bool:
    return all(getattr(record, name) == value for name, value in projection.items())


class InMemoryRunStore:
    """A locked, capacity-bounded run state machine with coordinated TTL candidates."""

    def __init__(
        self,
        *,
        max_runs: int,
        retention_seconds: int,
        clock: WallClock | object = _utc_now,
    ) -> None:
        if type(max_runs) is not int or max_runs <= 0:
            raise ValueError("max_runs must be a positive integer")
        if type(retention_seconds) is not int or retention_seconds <= 0:
            raise ValueError("retention_seconds must be a positive integer")
        if callable(clock):
            self._clock = clock
        else:
            now = getattr(clock, "now", None)
            if not callable(now):
                raise TypeError("clock must be callable or expose now()")
            self._clock = now
        self._max_runs = max_runs
        self._retention_seconds = retention_seconds
        self._lock = asyncio.Lock()
        self._records: dict[str, RunRecord] = {}

    def _now(self) -> datetime:
        return _require_utc(self._clock())

    def _is_expired(self, record: RunRecord, now: datetime) -> bool:
        return (
            record.lifecycle_status is RunLifecycleStatus.TERMINAL
            and record.terminal_at is not None
            and (now - record.terminal_at).total_seconds() >= self._retention_seconds
        )

    async def create(self, run_id: str, query: str) -> RunRecord:
        now = self._now()
        candidate = RunRecord(
            run_id=run_id,
            query=query,
            lifecycle_status=RunLifecycleStatus.QUEUED,
            created_at=now,
        )
        async with self._lock:
            if run_id in self._records:
                raise RunAlreadyExists()
            if len(self._records) >= self._max_runs:
                raise RunCapacityExceeded()
            self._records[run_id] = candidate
            return candidate

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            try:
                return self._records[run_id]
            except KeyError:
                raise RunNotFound() from None

    async def mark_running(self, run_id: str) -> RunRecord:
        now = self._now()
        async with self._lock:
            record = self._require(run_id)
            if record.lifecycle_status is not RunLifecycleStatus.QUEUED:
                raise RunStateConflict()
            updated = record.model_copy(
                update={
                    "lifecycle_status": RunLifecycleStatus.RUNNING,
                    "started_at": now,
                }
            )
            self._records[run_id] = updated
            return updated

    async def complete(self, run_id: str, result: AgentRunResult) -> RunRecord:
        now = self._now()
        if result.run_id != run_id:
            raise RunStateConflict()
        projection = _terminal_projection(result)
        async with self._lock:
            record = self._require(run_id)
            if record.lifecycle_status is RunLifecycleStatus.TERMINAL:
                if _terminal_record_matches(record, projection):
                    return record
                raise RunStateConflict()
            if record.lifecycle_status is not RunLifecycleStatus.RUNNING:
                raise RunStateConflict()
            updated = record.model_copy(
                update={
                    "lifecycle_status": RunLifecycleStatus.TERMINAL,
                    "terminal_at": now,
                    **projection,
                }
            )
            self._records[run_id] = updated
            return updated

    async def expired_terminal_ids(self) -> tuple[str, ...]:
        now = self._now()
        async with self._lock:
            return tuple(
                run_id for run_id, record in self._records.items() if self._is_expired(record, now)
            )

    async def delete_queued(self, run_id: str) -> bool:
        async with self._lock:
            record = self._require(run_id)
            if record.lifecycle_status is not RunLifecycleStatus.QUEUED:
                return False
            del self._records[run_id]
            return True

    async def delete_terminal(self, run_id: str) -> bool:
        async with self._lock:
            record = self._require(run_id)
            if record.lifecycle_status is not RunLifecycleStatus.TERMINAL:
                return False
            del self._records[run_id]
            return True

    def _require(self, run_id: str) -> RunRecord:
        try:
            return self._records[run_id]
        except KeyError:
            raise RunNotFound() from None


def deterministic_failure_result(
    *,
    run_id: str,
    status: FinalStatus,
    reason: StopReason,
    governance: GovernanceSnapshot,
    safe_trace: SafeTrace,
) -> AgentRunResult:
    answer_text = (
        "分析因执行超时终止。" if reason is StopReason.TASK_TIMEOUT else "分析因内部受控错误终止。"
    )
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
        governance=_copy_model(governance),
        final_answer=FinalAnswer(
            status=status,
            stop_reason=reason,
            answer=answer_text,
        ),
        safe_trace=_copy_model(safe_trace),
    )


def terminal_event_data(result: AgentRunResult) -> Mapping[str, str]:
    return {
        "final_status": result.final_answer.status.value,
        "stop_reason": result.final_answer.stop_reason.value,
    }


class AnalysisRunner:
    """Own bounded background tasks without tying their lifetime to consumers."""

    def __init__(
        self,
        *,
        settings: AgentRuntimeSettings,
        runs: RunStore,
        events: EventStore,
        context_factory: ContextFactory,
        agent_executor: AgentExecutor = run_agent,
        timeout_factory: TimeoutFactory = asyncio.timeout,  # type: ignore[assignment]
        id_factory: RunIdFactory = lambda: uuid4().hex,
    ) -> None:
        self._runs = runs
        self._events = events
        self._context_factory = context_factory
        self._agent_executor = agent_executor
        self._timeout_factory = timeout_factory
        self._timeout_seconds = float(settings.timeout_seconds)
        self._id_factory = id_factory
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self._prune_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._terminalizing: set[str] = set()
        self._run_consistency_failures: set[str] = set()
        self._consistency_failed = False
        self._accepting = True

    async def submit(self, query: str) -> RunRecord:
        async with self._prune_lock:
            if self._consistency_failed:
                raise RunConsistencyError()
            if not self._accepting:
                raise RunnerShutdown()
            try:
                await self._prune_locked()
            except RunConsistencyError:
                self._enter_fail_stop()
                raise
            run_id = self._id_factory()
            run_existed_before = await self._run_exists_before_submission(run_id)
            try:
                record = await self._runs.create(run_id, query)
            except (RunAlreadyExists, RunCapacityExceeded):
                raise
            except BaseException as error:
                coordination = _DelayedCancellation()
                coordination.observe(error)
                owned_run_exists = not run_existed_before and await self._run_exists_after_failure(
                    run_id, coordination
                )
                if owned_run_exists and not await self._delete_owned_queued_run(
                    run_id, coordination
                ):
                    self._raise_coordination_failure(coordination)
                coordination.raise_if_pending()
                raise RunSubmissionFailed() from None
            try:
                event_existed_before = await self._event_exists_before_submission(run_id)
            except BaseException as error:
                coordination = _DelayedCancellation()
                coordination.observe(error)
                if not await self._delete_owned_queued_run(run_id, coordination):
                    self._raise_coordination_failure(coordination)
                coordination.raise_if_pending()
                if isinstance(error, RunConsistencyError):
                    raise
                raise RunSubmissionFailed() from None
            events_created = False
            try:
                await self._events.create_run(run_id)
                events_created = True
                await self._events.emit(
                    run_id,
                    "runtime",
                    "run.created",
                    {"status": "queued"},
                )
            except BaseException as error:
                coordination = _DelayedCancellation()
                coordination.observe(error)
                if not event_existed_before and not events_created:
                    events_created = await self._event_exists_after_failure(run_id, coordination)
                await self._rollback_submission(
                    run_id,
                    events_created=events_created,
                    coordination=coordination,
                )
                raise RunSubmissionFailed() from None
            if self._consistency_failed:
                await self._rollback_submission(run_id, events_created=True)
                raise RunConsistencyError()
            coroutine = self._execute(run_id, query)
            try:
                task = asyncio.create_task(coroutine, name=f"analysis:{run_id}")
            except BaseException as error:
                coroutine.close()
                coordination = _DelayedCancellation()
                coordination.observe(error)
                await self._rollback_submission(
                    run_id,
                    events_created=True,
                    coordination=coordination,
                )
                raise RunSubmissionFailed() from None
            self._tasks[run_id] = task

            def task_done(done: asyncio.Task[None]) -> None:
                self._task_done(run_id, done)

            task.add_done_callback(task_done)
            return record

    async def get(self, run_id: str) -> RunRecord:
        return await self._runs.get(run_id)

    async def wait(self, run_id: str) -> RunRecord:
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except RunConsistencyError:
                self._run_consistency_failures.add(run_id)
            except Exception:
                self._enter_fail_stop(run_id)
        if run_id in self._run_consistency_failures:
            raise RunConsistencyError()
        return await self._runs.get(run_id)

    async def shutdown(self) -> None:
        async with self._prune_lock:
            self._accepting = False
            tasks = tuple(task for task in self._tasks.values() if not task.done())
            cancellable = tuple(
                task
                for run_id, task in self._tasks.items()
                if not task.done() and run_id not in self._terminalizing
            )
        for task in cancellable:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, run_id: str, query: str) -> None:
        try:
            async with self._semaphore:
                await self._ensure_running(run_id)
                context: AgentContext | None = None
                result: AgentRunResult | None = None
                try:
                    await self._events.emit(
                        run_id,
                        "runtime",
                        "run.started",
                        {"status": "running"},
                    )
                except asyncio.CancelledError:
                    if _task_is_cancelling():
                        raise
                    result = self._failure_result(
                        run_id,
                        context,
                        status=FinalStatus.INTERNAL_ERROR,
                        reason=StopReason.INTERNAL_ERROR,
                    )
                except Exception:
                    result = self._failure_result(
                        run_id,
                        context,
                        status=FinalStatus.INTERNAL_ERROR,
                        reason=StopReason.INTERNAL_ERROR,
                    )
                if result is None:
                    try:
                        context = self._context_factory(run_id)
                        async with self._timeout_factory(self._timeout_seconds):
                            candidate = await self._agent_executor(
                                run_id=run_id,
                                query=query,
                                context=context,
                            )
                    except TimeoutError:
                        result = self._failure_result(
                            run_id,
                            context,
                            status=FinalStatus.EXECUTION_FAILED,
                            reason=StopReason.TASK_TIMEOUT,
                        )
                    except asyncio.CancelledError:
                        if _task_is_cancelling():
                            raise
                        result = self._failure_result(
                            run_id,
                            context,
                            status=FinalStatus.INTERNAL_ERROR,
                            reason=StopReason.INTERNAL_ERROR,
                        )
                    except Exception:
                        result = self._failure_result(
                            run_id,
                            context,
                            status=FinalStatus.INTERNAL_ERROR,
                            reason=StopReason.INTERNAL_ERROR,
                        )
                    else:
                        result = self._validated_result(run_id, candidate, context)
                self._terminalizing.add(run_id)
                try:
                    await self._ensure_run_terminal(run_id, result)
                    await self._ensure_event_terminal(run_id, result)
                finally:
                    self._terminalizing.discard(run_id)
            async with self._prune_lock:
                await self._prune_locked()
        except asyncio.CancelledError:
            raise
        except RunConsistencyError:
            self._enter_fail_stop(run_id)
            raise
        except Exception:
            self._enter_fail_stop(run_id)
            raise RunConsistencyError() from None

    def _validated_result(
        self,
        run_id: str,
        candidate: object,
        context: AgentContext | None,
    ) -> AgentRunResult:
        if type(candidate) is AgentRunResult:
            try:
                owned = AgentRunResult.model_validate_json(candidate.model_dump_json())
            except Exception:
                pass
            else:
                if owned.run_id == run_id:
                    return owned
        return self._failure_result(
            run_id,
            context,
            status=FinalStatus.INTERNAL_ERROR,
            reason=StopReason.INTERNAL_ERROR,
        )

    async def _ensure_running(self, run_id: str) -> None:
        for _ in range(3):
            try:
                record = await self._runs.mark_running(run_id)
            except asyncio.CancelledError:
                if _task_is_cancelling():
                    raise
                record = await self._read_run_for_reconciliation(run_id)
            except Exception:
                record = await self._read_run_for_reconciliation(run_id)
            if record.lifecycle_status is RunLifecycleStatus.RUNNING:
                return
            if record.lifecycle_status is not RunLifecycleStatus.QUEUED:
                break
        raise RunConsistencyError()

    async def _ensure_run_terminal(self, run_id: str, result: AgentRunResult) -> None:
        projection = _terminal_projection(result)
        for _ in range(3):
            try:
                record = await self._runs.complete(run_id, result)
            except asyncio.CancelledError:
                if _task_is_cancelling():
                    raise
                record = await self._read_run_for_reconciliation(run_id)
            except Exception:
                record = await self._read_run_for_reconciliation(run_id)
            if record.lifecycle_status is RunLifecycleStatus.TERMINAL and _terminal_record_matches(
                record, projection
            ):
                return
            if record.lifecycle_status is not RunLifecycleStatus.RUNNING:
                break
        raise RunConsistencyError()

    async def _read_run_for_reconciliation(self, run_id: str) -> RunRecord:
        try:
            return await self._runs.get(run_id)
        except asyncio.CancelledError:
            if _task_is_cancelling():
                raise
            raise RunConsistencyError() from None
        except Exception:
            raise RunConsistencyError() from None

    async def _ensure_event_terminal(self, run_id: str, result: AgentRunResult) -> None:
        expected = dict(terminal_event_data(result))
        for _ in range(3):
            try:
                terminal_exists = await self._events.has_terminal(run_id)
            except asyncio.CancelledError:
                if _task_is_cancelling():
                    raise
                terminal_exists = False
            except Exception:
                terminal_exists = False
            if terminal_exists:
                matches = await self._terminal_event_matches(run_id, expected)
                if matches is True:
                    return
                if matches is False:
                    continue
            try:
                event = await self._events.emit_terminal(run_id, expected)
            except asyncio.CancelledError:
                if _task_is_cancelling():
                    raise
                continue
            except Exception:
                continue
            if self._is_expected_terminal_event(event, expected):
                return
        try:
            if await self._events.has_terminal(run_id) and await self._terminal_event_matches(
                run_id, expected
            ):
                return
        except asyncio.CancelledError:
            if _task_is_cancelling():
                raise
        except Exception:
            pass
        raise RunConsistencyError()

    async def _terminal_event_matches(
        self,
        run_id: str,
        expected: Mapping[str, str],
    ) -> bool | None:
        try:
            high_water_mark = await self._events.high_water_mark(run_id)
            if high_water_mark == 0:
                return False
            snapshot = await self._events.replay_snapshot(
                run_id,
                high_water_mark=high_water_mark,
            )
        except asyncio.CancelledError:
            if _task_is_cancelling():
                raise
            return None
        except Exception:
            return None
        if len(snapshot) != high_water_mark:
            return False
        if any(event.sequence != index for index, event in enumerate(snapshot, start=1)):
            return False
        if any(event.run_id != run_id for event in snapshot):
            return False
        if any(event.type == "run.terminal" for event in snapshot[:-1]):
            return False
        return self._is_expected_terminal_event(snapshot[-1], expected)

    @staticmethod
    def _is_expected_terminal_event(
        event: object,
        expected: Mapping[str, str],
    ) -> bool:
        return (
            type(event) is RunEvent
            and event.type == "run.terminal"
            and dict(event.data) == dict(expected)
        )

    @staticmethod
    def _failure_result(
        run_id: str,
        context: AgentContext | None,
        *,
        status: FinalStatus,
        reason: StopReason,
    ) -> AgentRunResult:
        governance = GovernanceSnapshot()
        trace = SafeTrace()
        if context is not None:
            with suppress(Exception):
                governance = context.budget.snapshot
            with suppress(Exception):
                trace = context.trace_recorder.snapshot()
        return deterministic_failure_result(
            run_id=run_id,
            status=status,
            reason=reason,
            governance=governance,
            safe_trace=trace,
        )

    async def _event_exists_before_submission(self, run_id: str) -> bool:
        try:
            await self._events.high_water_mark(run_id)
        except EventRunNotFound:
            return False
        except asyncio.CancelledError:
            if _task_is_cancelling():
                raise
            self._enter_fail_stop()
            raise RunConsistencyError() from None
        except Exception:
            self._enter_fail_stop()
            raise RunConsistencyError() from None
        return True

    async def _run_exists_before_submission(self, run_id: str) -> bool:
        try:
            await self._runs.get(run_id)
        except RunNotFound:
            return False
        except asyncio.CancelledError:
            if _task_is_cancelling():
                raise
            self._enter_fail_stop()
            raise RunConsistencyError() from None
        except Exception:
            self._enter_fail_stop()
            raise RunConsistencyError() from None
        return True

    async def _run_exists_after_failure(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> bool:
        return await self._run_record_after_delete(run_id, coordination) is not None

    async def _event_high_water_after_failure(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> int | None:
        for _ in range(3):
            try:
                return await self._events.high_water_mark(run_id)
            except EventRunNotFound:
                return None
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
        self._raise_coordination_failure(coordination, run_id)
        raise AssertionError("unreachable")

    async def _event_exists_after_failure(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> bool:
        return await self._event_high_water_after_failure(run_id, coordination) is not None

    async def _rollback_submission(
        self,
        run_id: str,
        *,
        events_created: bool,
        coordination: _DelayedCancellation | None = None,
    ) -> None:
        state = coordination or _DelayedCancellation()
        if events_created and not await self._delete_event_for_rollback(run_id, state):
            self._raise_coordination_failure(state, run_id)
        if not await self._delete_owned_queued_run(run_id, state):
            await self._restore_owned_event_run(run_id, state)
            self._raise_coordination_failure(state, run_id)
        state.raise_if_pending()

    async def _delete_event_for_rollback(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> bool:
        for _ in range(3):
            try:
                await self._events.delete_run(run_id, allow_unstarted=True)
            except EventRunNotFound:
                return True
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
            if not await self._event_exists_after_failure(run_id, coordination):
                return True
        return False

    async def _delete_owned_queued_run(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> bool:
        for _ in range(3):
            try:
                await self._runs.delete_queued(run_id)
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
            record = await self._run_record_after_delete(run_id, coordination)
            if record is None:
                return True
            if record.lifecycle_status is not RunLifecycleStatus.QUEUED:
                self._raise_coordination_failure(coordination, run_id)
        return False

    async def _delete_terminal_run(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> bool:
        for _ in range(3):
            try:
                await self._runs.delete_terminal(run_id)
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
            record = await self._run_record_after_delete(run_id, coordination)
            if record is None:
                return True
            if record.lifecycle_status is not RunLifecycleStatus.TERMINAL:
                self._raise_coordination_failure(coordination, run_id)
        return False

    async def _run_record_after_delete(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> RunRecord | None:
        for _ in range(3):
            try:
                return await self._runs.get(run_id)
            except RunNotFound:
                return None
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
        self._raise_coordination_failure(coordination, run_id)
        raise AssertionError("unreachable")

    async def _restore_owned_event_run(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> None:
        high_water_mark = await self._event_high_water_after_failure(run_id, coordination)
        for _ in range(3):
            if high_water_mark is not None:
                break
            try:
                await self._events.create_run(run_id)
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
            high_water_mark = await self._event_high_water_after_failure(run_id, coordination)
        if high_water_mark is None:
            self._raise_coordination_failure(coordination, run_id)
        for _ in range(3):
            if high_water_mark == 1 and await self._restored_created_event_matches(
                run_id, coordination
            ):
                return
            if high_water_mark != 0:
                self._raise_coordination_failure(coordination, run_id)
            try:
                await self._events.emit(
                    run_id,
                    "runtime",
                    "run.created",
                    {"status": "queued"},
                )
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
            high_water_mark = await self._event_high_water_after_failure(run_id, coordination)
            if high_water_mark is None:
                self._raise_coordination_failure(coordination, run_id)
        self._raise_coordination_failure(coordination, run_id)

    async def _restored_created_event_matches(
        self,
        run_id: str,
        coordination: _DelayedCancellation,
    ) -> bool:
        for _ in range(3):
            try:
                snapshot = await self._events.replay_snapshot(run_id, high_water_mark=1)
            except asyncio.CancelledError as error:
                coordination.observe(error)
            except Exception:
                pass
            else:
                return (
                    len(snapshot) == 1
                    and snapshot[0].run_id == run_id
                    and snapshot[0].sequence == 1
                    and snapshot[0].type == "run.created"
                    and dict(snapshot[0].data) == {"status": "queued"}
                )
        self._raise_coordination_failure(coordination, run_id)
        raise AssertionError("unreachable")

    def _raise_coordination_failure(
        self,
        coordination: _DelayedCancellation,
        run_id: str | None = None,
    ) -> None:
        self._enter_fail_stop(run_id)
        coordination.raise_if_pending()
        raise RunConsistencyError()

    async def _prune_locked(self) -> None:
        try:
            expired = await self._runs.expired_terminal_ids()
        except asyncio.CancelledError:
            if _task_is_cancelling():
                raise
            raise RunConsistencyError() from None
        except Exception:
            raise RunConsistencyError() from None
        for run_id in expired:
            coordination = _DelayedCancellation()
            deleted = False
            for _ in range(3):
                try:
                    await self._events.delete_run(run_id)
                except EventRunNotFound:
                    deleted = True
                except asyncio.CancelledError as error:
                    coordination.observe(error)
                except Exception:
                    pass
                if deleted or not await self._event_exists_after_failure(run_id, coordination):
                    deleted = True
                    break
            if deleted and not await self._delete_terminal_run(run_id, coordination):
                self._raise_coordination_failure(coordination, run_id)
            coordination.raise_if_pending()

    def _enter_fail_stop(self, run_id: str | None = None) -> None:
        self._accepting = False
        self._consistency_failed = True
        if run_id is not None:
            self._run_consistency_failures.add(run_id)

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            del self._tasks[run_id]
        if not task.cancelled():
            with suppress(Exception):
                task.exception()


__all__ = [
    "AnalysisRunner",
    "InMemoryRunStore",
    "RunAlreadyExists",
    "RunCapacityExceeded",
    "RunConsistencyError",
    "RunNotFound",
    "RunRecord",
    "RunStateConflict",
    "RunStore",
    "RunStoreError",
    "RunSubmissionFailed",
    "RunnerShutdown",
    "TimeoutFactory",
    "deterministic_failure_result",
    "terminal_event_data",
]
