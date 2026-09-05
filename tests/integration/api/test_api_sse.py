from __future__ import annotations

import asyncio
import gc
import inspect
import json
import re
import threading
import traceback
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol, cast
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlglot import Tokenizer, TokenType
from sqlglot.errors import TokenError
from sse_starlette.event import ServerSentEvent

import governed_analytics.api.dependencies as dependencies
from governed_analytics.api.app import create_app
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.models.agent_fixtures import AgentScripts
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.runtime.events import InMemoryEventStore, RunEvent
from governed_analytics.runtime.runs import AnalysisRunner
from governed_analytics.safety.sql_policy import (
    SqlPolicyError,
    SqlRejectionCode,
    ValidatedSql,
    validate_sql,
)
from governed_analytics.tools.contracts import QueryResult
from governed_analytics.tools.execution import AsyncEngineSqlExecutionBackend

ATTRIBUTION_QUERY = (
    "比较 2026-06-01 至 06-08 与 06-08 至 06-15 的 "
    "GMV，并按区域、SKU、客户分群解释下降。"  # noqa: RUF001
)

_COMPARISON_SQL = """
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

_REGION_SQL = """
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
  and o.ordered_at >= cast(:previous_start as timestamptz)
  and o.ordered_at < cast(:current_end as timestamptz)
group by o.region
order by gmv_loss desc, region asc
fetch first 10 rows only
"""

_SKU_SQL = """
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
  and o.ordered_at >= cast(:previous_start as timestamptz)
  and o.ordered_at < cast(:current_end as timestamptz)
group by p.sku
order by gmv_loss desc, sku asc
fetch first 10 rows only
"""

_SEGMENT_SQL = """
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
    and o.ordered_at >= cast(:previous_start as timestamptz)
    and o.ordered_at < cast(:current_end as timestamptz)
  group by c.segment
)
select segment, previous_gmv, current_gmv, current_gmv - previous_gmv as delta
from segment_gmv
order by delta asc, segment asc
fetch first 10 rows only
"""

_PARAMETERS = {
    "previous_start": "2026-06-01T00:00:00Z",
    "previous_end": "2026-06-08T00:00:00Z",
    "current_start": "2026-06-08T00:00:00Z",
    "current_end": "2026-06-15T00:00:00Z",
}

_FORBIDDEN_KEYS = frozenset(
    {
        "rows",
        "row",
        "result",
        "raw_result",
        "query_result",
        "raw_rows",
        "records",
        "sql",
        "raw_sql",
        "sql_text",
        "statement",
        "query",
        "parameters",
        "params",
        "prompt",
        "payload",
        "endpoint",
        "provider_raw",
        "api_key",
        "secret",
        "password",
        "authorization",
        "bearer",
        "access_token",
        "credentials",
        "token",
    }
)

_CREATE_KEYS = frozenset(
    {
        "run_id",
        "lifecycle_status",
        "status_url",
        "events_url",
        "trace_url",
    }
)
_EVENT_KEYS = frozenset({"event_id", "sequence", "run_id", "timestamp", "node", "type", "data"})
_EVENT_DATA_KEYS = {
    "run.created": frozenset({"status"}),
    "run.started": frozenset({"status"}),
    "behavior.decided": frozenset({"action", "reason_code", "missing_fields"}),
    "context.retrieved": frozenset({"metric_count", "table_count", "success"}),
    "plan.created": frozenset(
        {"plan_id", "revision", "analysis_type", "metric_id", "hypothesis_ids"}
    ),
    "hypothesis.updated": frozenset({"hypothesis_id", "status"}),
    "tool.started": frozenset({"tool_name", "purpose", "contract_id"}),
    "tool.completed": frozenset(
        {"tool_name", "purpose", "query_id", "columns", "row_count", "possibly_truncated"}
    ),
    "tool.failed": frozenset({"tool_name", "purpose", "safe_error"}),
    "observation.validated": frozenset({"contract_id", "valid", "error_code", "repairable"}),
    "evidence.assessed": frozenset({"verified_count", "gaps", "partial", "complete"}),
    "repair.started": frozenset({"repair_count", "error_code", "success"}),
    "repair.completed": frozenset({"repair_count", "error_code", "success"}),
    "budget.warning": frozenset(
        {
            "reason",
            "llm_calls",
            "tool_calls",
            "execute_calls",
            "profile_calls",
            "repair_count",
            "committed_cost_cny",
        }
    ),
    "run.terminal": frozenset({"final_status", "stop_reason"}),
}
_STATUS_KEYS = frozenset(
    {
        "run_id",
        "lifecycle_status",
        "final_status",
        "answer",
        "evidence",
        "limitations",
        "stop_reason",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "observation_id",
        "hypothesis_id",
        "contract_id",
        "query_id",
        "claim_key",
        "dimensions",
        "stance",
        "numeric_value",
        "unit",
        "limitations",
    }
)
_TRACE_KEYS = frozenset(
    {
        "run_id",
        "snapshot_complete",
        "nodes",
        "model_calls",
        "tool_calls",
        "evidence_gaps",
        "repair_count",
        "model_call_count",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "committed_cost_cny",
        "stop_reason",
    }
)
_NODE_TRACE_KEYS = frozenset({"node", "duration_ms", "outcome"})
_MODEL_TRACE_KEYS = frozenset(
    {
        "purpose",
        "provider_model",
        "outcome",
        "safe_error",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "finish_reason",
        "output_truncated",
        "estimated_cost_cny",
    }
)
_TOOL_TRACE_KEYS = frozenset(
    {
        "tool_name",
        "purpose",
        "safe_arguments",
        "query_id",
        "columns",
        "row_count",
        "possibly_truncated",
        "safe_error",
    }
)
_SAFE_ARGUMENT_NAMES_BY_TOOL = {
    "execute_sql": frozenset({"contract_id", "hypothesis_id"}),
    "metric_lookup": frozenset(),
    "profile": frozenset(
        {
            "column_name",
            "filter_columns",
            "has_time_window",
            "limit",
            "operation",
            "table_name",
            "time_column",
        }
    ),
    "schema_lookup": frozenset(),
}
_CREDENTIAL_CANARY = "task11-fix1-credential-canary-never-expose"
_SAFE_OUTPUT_ERROR = "serialized response violated safe-output contract"
_SSE_DEADLINE_ERROR = "SSE response exceeded deterministic test deadline"
_GATE_DEADLINE_ERROR = "SSE coordination gate exceeded deterministic test deadline"
_CLEANUP_DEADLINE_ERROR = "SSE cleanup exceeded deterministic test deadline"
_SSE_DEADLINE_SECONDS = 8.0
_GATE_DEADLINE_SECONDS = 5.0
_CLEANUP_DEADLINE_SECONDS = 0.25


class _JsonResponse(Protocol):
    @property
    def text(self) -> str: ...

    def json(self) -> object: ...


def _action(contract_id: str, hypothesis_id: str, sql: str) -> dict[str, object]:
    return {
        "action_type": "execute_sql",
        "purpose": contract_id,
        "arguments": {"sql": sql, "parameters": dict(_PARAMETERS)},
        "hypothesis_id": hypothesis_id,
        "contract_id": contract_id,
        "expected_evidence": "contracted numeric result",
    }


API_SCRIPTS = cast(
    AgentScripts,
    {
        ATTRIBUTION_QUERY.casefold(): {
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
                    "plan_id": "api-gmv-attribution-integration",
                    "revision": 1,
                    "metric_id": "gmv",
                    "metric_version": "1.0.0",
                    "analysis_type": "attribution",
                    "windows": (
                        {
                            "label": "previous",
                            "start_at": _PARAMETERS["previous_start"],
                            "end_at": _PARAMETERS["previous_end"],
                        },
                        {
                            "label": "current",
                            "start_at": _PARAMETERS["current_start"],
                            "end_at": _PARAMETERS["current_end"],
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
                },
            ),
            "action": (
                _action("gmv_comparison", "confirm_decline", _COMPARISON_SQL),
                _action("region_contribution", "region_contribution", _REGION_SQL),
                _action("sku_contribution", "sku_contribution", _SKU_SQL),
                _action("segment_contribution", "segment_contribution", _SEGMENT_SQL),
            ),
            "synthesis": (
                {
                    "status": "completed",
                    "stop_reason": "answer_complete",
                    "answer": "GMV API 数据库链路验证完成。",
                    "evidence_ids": (),
                },
            ),
        }
    },
)


@dataclass(frozen=True, slots=True)
class _ResourceEvent:
    name: str
    thread_id: int
    loop_id: int


@dataclass(slots=True)
class _LifespanProbe:
    resource_events: list[_ResourceEvent] = field(default_factory=list)
    execution_gate: asyncio.Event | None = None
    live_read_gate: asyncio.Event | None = None
    event_store: _SpyEventStore | None = None
    open_high_water: int = 0
    open_nonterminal: bool = False
    live_wait_started: bool = False
    active_leases: int = 0

    def record(self, name: str) -> None:
        self.resource_events.append(
            _ResourceEvent(
                name=name,
                thread_id=threading.get_ident(),
                loop_id=id(asyncio.get_running_loop()),
            )
        )


class _SpyEngine:
    def __init__(self, delegate: AsyncEngine, probe: _LifespanProbe) -> None:
        self._delegate = delegate
        self._probe = probe
        self.connect_calls = 0
        self.dispose_calls = 0

    def connect(self) -> AsyncConnection:
        self.connect_calls += 1
        self._probe.record("engine.connect")
        return self._delegate.connect()

    async def dispose(self) -> None:
        self.dispose_calls += 1
        self._probe.record("engine.dispose")
        await self._delegate.dispose()


@dataclass(slots=True)
class _OwnedTaskHandle:
    task: asyncio.Task[object] | None = None
    reclaimed: bool = False

    @property
    def available(self) -> bool:
        return self.task is None and not self.reclaimed

    def bind(self, task: asyncio.Task[object]) -> None:
        _safe_output_require(self.task is None)
        self.task = task

    async def reclaim(self) -> None:
        _safe_output_require(self.task is not None)
        assert self.task is not None
        await asyncio.gather(self.task, return_exceptions=True)
        self.reclaimed = True


async def _cancel_owned_task[T](task: asyncio.Task[T]) -> None:
    task.cancel()
    try:
        await asyncio.sleep(0)
    finally:
        if task.done():
            _observe_background_task(task)
        else:
            task.add_done_callback(_observe_background_task)


async def _await_with_deadline[T](
    operation: Awaitable[T],
    *,
    seconds: float,
    failure_message: str,
    owned_task_name: str = "sse-test-deadline",
    owned_task_handle: _OwnedTaskHandle | None = None,
) -> T:
    loop = asyncio.get_running_loop()
    expires_at = loop.time() + seconds
    owned = not asyncio.isfuture(operation)
    if owned_task_handle is not None and (not owned or not owned_task_handle.available):
        if owned and inspect.iscoroutine(operation):
            operation.close()
        raise AssertionError(_SAFE_OUTPUT_ERROR)
    if owned:
        async def await_owned_operation() -> T:
            return await operation

        owned_task = asyncio.create_task(
            await_owned_operation(), name=owned_task_name
        )
        if owned_task_handle is not None:
            owned_task_handle.bind(cast(asyncio.Task[object], owned_task))
        task: asyncio.Future[T] = owned_task
    else:
        _safe_output_require(owned_task_handle is None)
        task = cast(asyncio.Future[T], operation)
    try:
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.0, expires_at - loop.time()),
        )
    except BaseException:
        if task.done():
            _observe_background_task(task)
        elif owned:
            await _cancel_owned_task(cast(asyncio.Task[T], task))
        raise
    if task in done and loop.time() <= expires_at:
        return await task
    if not task.done() and owned:
        await _cancel_owned_task(cast(asyncio.Task[T], task))
    elif task.done():
        _observe_background_task(task)
    raise AssertionError(failure_message)


def _observe_background_task[T](task: asyncio.Future[T]) -> None:
    if not task.cancelled():
        task.exception()


class _GatedBackend:
    def __init__(self, engine: AsyncEngine, probe: _LifespanProbe) -> None:
        self._delegate = AsyncEngineSqlExecutionBackend(engine)
        self._probe = probe

    async def execute(
        self,
        validated: ValidatedSql,
        parameters: tuple[object, ...],
    ) -> QueryResult:
        self._probe.record("backend.execute")
        execution_gate = self._probe.execution_gate
        live_read_gate = self._probe.live_read_gate
        assert execution_gate is not None
        assert live_read_gate is not None
        await _await_with_deadline(
            execution_gate.wait(),
            seconds=_GATE_DEADLINE_SECONDS,
            failure_message=_GATE_DEADLINE_ERROR,
        )
        await _await_with_deadline(
            live_read_gate.wait(),
            seconds=_GATE_DEADLINE_SECONDS,
            failure_message=_GATE_DEADLINE_ERROR,
        )
        return await self._delegate.execute(validated, parameters)


class _TrackedEventStream:
    def __init__(
        self,
        delegate: AsyncIterator[RunEvent],
        probe: _LifespanProbe,
        *,
        open_high_water: int,
    ) -> None:
        self._delegate = delegate
        self._probe = probe
        self._open_high_water = open_high_water
        self._last_sequence = 0
        self._released = False

    def __aiter__(self) -> _TrackedEventStream:
        return self

    async def __anext__(self) -> RunEvent:
        if not self._probe.live_wait_started and self._last_sequence >= self._open_high_water:
            self._probe.live_wait_started = True
            self._probe.record("events.live_wait")
            live_read_gate = self._probe.live_read_gate
            assert live_read_gate is not None
            live_read_gate.set()
        try:
            event = await anext(self._delegate)
        except StopAsyncIteration:
            await self._release()
            raise
        self._last_sequence = event.sequence
        return event

    async def aclose(self) -> None:
        close = getattr(self._delegate, "aclose", None)
        try:
            if callable(close):
                await close()
        finally:
            await self._release()

    async def _release(self) -> None:
        if self._released:
            return
        self._released = True
        self._probe.active_leases -= 1
        self._probe.record("events.lease_released")


class _SpyEventStore:
    def __init__(self, probe: _LifespanProbe) -> None:
        self._delegate = InMemoryEventStore()
        self._probe = probe

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def create_unstarted_run(self, run_id: str) -> None:
        await self._delegate.create_run(run_id)
        await self._delegate.emit(
            run_id,
            "runtime",
            "run.created",
            {"status": "queued"},
        )

    async def complete_probe_run(self, run_id: str) -> None:
        await self._delegate.emit_terminal(
            run_id,
            {"final_status": "completed", "stop_reason": "answer_complete"},
        )

    def reset_stream_gates(self) -> None:
        self._probe.execution_gate = asyncio.Event()
        self._probe.live_read_gate = asyncio.Event()
        self._probe.open_high_water = 0
        self._probe.open_nonterminal = False
        self._probe.live_wait_started = False

    async def open_stream(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
    ) -> AsyncIterator[RunEvent]:
        high_water = await self._delegate.high_water_mark(run_id)
        self._probe.open_high_water = high_water
        self._probe.open_nonterminal = not await self._delegate.has_terminal(run_id)
        opened = await self._delegate.open_stream(run_id, after_sequence=after_sequence)
        self._probe.active_leases += 1
        self._probe.record("events.open_stream")
        execution_gate = self._probe.execution_gate
        assert execution_gate is not None
        execution_gate.set()
        return _TrackedEventStream(
            opened,
            self._probe,
            open_high_water=high_water,
        )

    async def delete_run(
        self,
        run_id: str,
        *,
        allow_unstarted: bool = False,
        owner_token: object | None = None,
    ) -> bool:
        self._probe.record("events.delete_run")
        return await self._delegate.delete_run(
            run_id,
            allow_unstarted=allow_unstarted,
            owner_token=owner_token,
        )


class _SpyRunner(AnalysisRunner):
    def __init__(self, *, probe: _LifespanProbe, **kwargs: object) -> None:
        self._probe = probe
        self._probe.record("runner.construct")
        super().__init__(**kwargs)  # type: ignore[arg-type]

    async def shutdown(self) -> None:
        self._probe.record("runner.shutdown")
        await super().shutdown()


class _CloseRaisesStream:
    def __aiter__(self) -> _CloseRaisesStream:
        return self

    async def __anext__(self) -> RunEvent:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        raise RuntimeError("close failed")


class _CloseSucceedsStream:
    def __aiter__(self) -> _CloseSucceedsStream:
        return self

    async def __anext__(self) -> RunEvent:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        return None


class _BlockingCloseLines:
    def __init__(self) -> None:
        self._release_gate = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self._cancellation_changed = asyncio.Condition()
        self.cancellations = 0
        self.active_leases = 1

    def __aiter__(self) -> _BlockingCloseLines:
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        try:
            while not self._release_gate.is_set():
                try:
                    await self._release_gate.wait()
                except asyncio.CancelledError:
                    async with self._cancellation_changed:
                        self.cancellations += 1
                        self._cancellation_changed.notify_all()
        finally:
            self.active_leases -= 1
            self.close_finished.set()

    async def wait_for_cancellations(self, expected: int) -> None:
        async with self._cancellation_changed:
            await self._cancellation_changed.wait_for(lambda: self.cancellations >= expected)


@dataclass(frozen=True)
class _TrackedCloseProbeResult:
    runtime_error_observed: bool
    active_leases: int


async def _tracked_close_probe(delegate: AsyncIterator[RunEvent]) -> _TrackedCloseProbeResult:
    probe = _LifespanProbe(active_leases=1)
    tracked = _TrackedEventStream(delegate, probe, open_high_water=0)
    runtime_error_observed = False
    try:
        await tracked.aclose()
    except RuntimeError:
        runtime_error_observed = True
    return _TrackedCloseProbeResult(
        runtime_error_observed=runtime_error_observed,
        active_leases=probe.active_leases,
    )


async def _tracked_close_failure_probe() -> _TrackedCloseProbeResult:
    return await _tracked_close_probe(_CloseRaisesStream())


async def _tracked_close_success_probe() -> _TrackedCloseProbeResult:
    return await _tracked_close_probe(_CloseSucceedsStream())


async def _close_resists_repeated_cancellation_probe() -> bool:
    stream = _BlockingCloseLines()
    cleanup = asyncio.create_task(stream.aclose(), name="sse-cancel-resistance-probe")
    survived = True
    try:
        await _await_with_deadline(
            stream.close_started.wait(),
            seconds=_GATE_DEADLINE_SECONDS,
            failure_message=_GATE_DEADLINE_ERROR,
        )
        for expected_cancellations in range(1, 4):
            cleanup.cancel()
            await _await_with_deadline(
                stream.wait_for_cancellations(expected_cancellations),
                seconds=_GATE_DEADLINE_SECONDS,
                failure_message=_GATE_DEADLINE_ERROR,
            )
            survived = survived and not cleanup.done()
    finally:
        stream._release_gate.set()
        await _await_with_deadline(
            asyncio.gather(cleanup, return_exceptions=True),
            seconds=_GATE_DEADLINE_SECONDS,
            failure_message=_CLEANUP_DEADLINE_ERROR,
        )
    return (
        survived
        and stream.cancellations == 3
        and stream.close_finished.is_set()
        and stream.active_leases == 0
    )


async def _blocking_cleanup_fault_probe() -> bool:
    stream = _BlockingCloseLines()
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)
    cleanup_handle = _OwnedTaskHandle()
    observed = False
    try:
        await _collect_sse_lines(
            stream,
            cleanup_seconds=0.02,
            cleanup_task_handle=cleanup_handle,
        )
    except AssertionError as failure:
        await _await_with_deadline(
            stream.wait_for_cancellations(1),
            seconds=_GATE_DEADLINE_SECONDS,
            failure_message=_GATE_DEADLINE_ERROR,
        )
        introduced = _pending_tasks(current) - pending_before
        cleanup = cleanup_handle.task
        observed = (
            str(failure) == _CLEANUP_DEADLINE_ERROR
            and stream.cancellations == 1
            and len(introduced) == 1
            and cleanup is not None
            and cleanup in introduced
            and cleanup.get_name() == "sse-test-cleanup"
            and not cleanup.done()
            and stream.close_started.is_set()
            and not stream.close_finished.is_set()
            and stream.active_leases == 1
        )
    finally:
        stream._release_gate.set()
        if cleanup_handle.task is not None:
            await cleanup_handle.reclaim()
    return (
        observed
        and cleanup_handle.reclaimed
        and stream.close_finished.is_set()
        and stream.active_leases == 0
        and _pending_tasks(current) == pending_before
    )


async def _raise_operation_timeout() -> None:
    raise TimeoutError("operation-owned-timeout")


async def _close_with_deadline(
    close: Callable[[], Awaitable[object]],
    *,
    seconds: float,
    owned_task_handle: _OwnedTaskHandle | None = None,
) -> None:
    await _await_with_deadline(
        close(),
        seconds=seconds,
        failure_message=_CLEANUP_DEADLINE_ERROR,
        owned_task_name="sse-test-cleanup",
        owned_task_handle=owned_task_handle,
    )


async def _collect_sse_lines(
    lines: AsyncIterator[str],
    *,
    cleanup_seconds: float = _CLEANUP_DEADLINE_SECONDS,
    cleanup_task_handle: _OwnedTaskHandle | None = None,
) -> tuple[str, ...]:
    collected: list[str] = []
    try:
        async for line in lines:
            collected.append(line)
    finally:
        close = getattr(lines, "aclose", None)
        if callable(close):
            await _close_with_deadline(
                cast(Callable[[], Awaitable[object]], close),
                seconds=cleanup_seconds,
                owned_task_handle=cleanup_task_handle,
            )
    return tuple(collected)


async def _request_sse(app: FastAPI, events_url: str) -> tuple[int, str, tuple[str, ...]]:
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client,
        client.stream("GET", events_url) as response,
    ):
        lines = await _collect_sse_lines(response.aiter_lines())
        return response.status_code, response.headers["content-type"], lines


async def _consume_sse_with_deadline(
    app: FastAPI,
    events_url: str,
    probe: _LifespanProbe,
    database: DatabaseSettings,
    deadline_seconds: float = _SSE_DEADLINE_SECONDS,
) -> tuple[int, str, tuple[dict[str, object], ...]]:
    try:
        status_code, content_type, lines = await _await_with_deadline(
            _request_sse(app, events_url),
            seconds=deadline_seconds,
            failure_message=_SSE_DEADLINE_ERROR,
        )
        return status_code, content_type, _sse_payloads(iter(lines), database)
    finally:
        for gate in (probe.execution_gate, probe.live_read_gate):
            if gate is not None:
                gate.set()


async def _exercise_real_sse_timeout(
    app: FastAPI,
    event_store: _SpyEventStore,
    probe: _LifespanProbe,
    database: DatabaseSettings,
) -> bool:
    warmup_run_id = "integration-sse-watcher-warmup"
    await event_store.create_unstarted_run(warmup_run_id)
    await event_store.complete_probe_run(warmup_run_id)
    warmup_status, warmup_content_type, warmup_lines = await _await_with_deadline(
        _request_sse(app, f"/v1/analyses/{warmup_run_id}/events"),
        seconds=_GATE_DEADLINE_SECONDS,
        failure_message=_SSE_DEADLINE_ERROR,
    )
    warmup_events = _sse_payloads(iter(warmup_lines), database)
    _safe_output_require(warmup_status == 200)
    _safe_output_require(warmup_content_type.startswith("text/event-stream"))
    _safe_output_require(bool(warmup_events) and warmup_events[-1]["type"] == "run.terminal")
    _safe_output_require(probe.active_leases == 0)
    _safe_output_require(await event_store.delete_run(warmup_run_id, allow_unstarted=True))
    event_store.reset_stream_gates()

    run_id = "integration-real-sse-timeout"
    await event_store.create_unstarted_run(run_id)
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)
    worker_threads_before = frozenset(
        thread.ident for thread in threading.enumerate() if thread.is_alive()
    )
    consumer = asyncio.create_task(
        _request_sse(app, f"/v1/analyses/{run_id}/events"),
        name="sse-real-timeout-consumer",
    )
    execution_gate = probe.execution_gate
    live_read_gate = probe.live_read_gate
    _safe_output_require(execution_gate is not None and live_read_gate is not None)
    assert execution_gate is not None
    assert live_read_gate is not None
    await _await_with_deadline(
        execution_gate.wait(),
        seconds=_GATE_DEADLINE_SECONDS,
        failure_message=_GATE_DEADLINE_ERROR,
    )
    await _await_with_deadline(
        live_read_gate.wait(),
        seconds=_GATE_DEADLINE_SECONDS,
        failure_message=_GATE_DEADLINE_ERROR,
    )
    try:
        await _await_with_deadline(
            consumer,
            seconds=0.05,
            failure_message=_SSE_DEADLINE_ERROR,
        )
    except AssertionError as failure:
        _safe_output_require(str(failure) == _SSE_DEADLINE_ERROR)
    else:
        _safe_output_require(False)
    finally:
        execution_gate.set()
        live_read_gate.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    _safe_output_require(consumer.done())
    _safe_output_require(probe.open_nonterminal)
    _safe_output_require(probe.live_wait_started)
    _safe_output_require(probe.active_leases == 0)
    deleted = await event_store.delete_run(run_id, allow_unstarted=True)
    _safe_output_require(deleted)
    event_store.reset_stream_gates()

    pending_after: frozenset[asyncio.Task[object]] = frozenset()
    for _ in range(4):
        pending_after = _pending_tasks(current)
        if pending_after == pending_before:
            break
        checkpoint = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
        await checkpoint
    _safe_output_require(pending_after == pending_before)
    worker_threads_after = frozenset(
        thread.ident for thread in threading.enumerate() if thread.is_alive()
    )
    _safe_output_require(worker_threads_after == worker_threads_before)
    return True


def _pending_tasks(
    current: asyncio.Task[object] | None,
) -> frozenset[asyncio.Task[object]]:
    return frozenset(
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    )


def _sse_payloads(
    lines: Iterator[str],
    database: DatabaseSettings,
) -> tuple[dict[str, object], ...]:
    raw_lines = tuple(lines)
    _assert_serialized_output_has_no_secrets_or_sql("\n".join(raw_lines), database)
    for line in raw_lines:
        _assert_serialized_output_has_no_secrets_or_sql(line, database)

    payloads: list[dict[str, object]] = []
    frame: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    def finish_frame() -> None:
        if not frame:
            return
        _assert_safe_tree(frame)
        protocol_ids = tuple(value for name, value in frame if name == "id")
        protocol_events = tuple(value for name, value in frame if name == "event")
        data_values = tuple(value for name, value in frame if name == "data")
        _safe_output_require(len(protocol_ids) == 1)
        _safe_output_require(len(protocol_events) == 1)
        _safe_output_require(bool(data_values))
        protocol_id = protocol_ids[0]
        protocol_event = protocol_events[0]
        _safe_output_require(bool(protocol_id) and "\x00" not in protocol_id)
        _safe_output_require(protocol_id not in seen_ids)
        _safe_output_require(protocol_event in _EVENT_DATA_KEYS)
        serialized_data = "\n".join(data_values)
        _assert_serialized_output_has_no_secrets_or_sql(serialized_data, database)
        try:
            untrusted_payload = json.loads(serialized_data)
        except json.JSONDecodeError:
            raise AssertionError(_SAFE_OUTPUT_ERROR) from None
        _assert_safe_tree(untrusted_payload)
        payload = cast(
            dict[str, object],
            _assert_exact_keys(untrusted_payload, _EVENT_KEYS),
        )
        event_type = payload["type"]
        _safe_output_require(isinstance(event_type, str))
        event_type = cast(str, event_type)
        _safe_output_require(event_type == protocol_event)
        _assert_exact_keys(payload["data"], _EVENT_DATA_KEYS[event_type])
        _safe_output_require(payload["event_id"] == protocol_id)
        seen_ids.add(protocol_id)
        payloads.append(payload)
        frame.clear()

    for line in raw_lines:
        if line == "":
            finish_frame()
            continue
        if line.startswith(":"):
            _assert_serialized_output_has_no_secrets_or_sql(line[1:], database)
            continue
        _safe_output_require(":" in line)
        field_name, value = line.split(":", 1)
        if value.startswith(" "):
            value = value[1:]
        _assert_serialized_output_has_no_secrets_or_sql(field_name, database)
        _assert_serialized_output_has_no_secrets_or_sql(value, database)
        _safe_output_require(field_name in {"id", "event", "data", "retry"})
        _safe_output_require(field_name != "retry")
        frame.append((field_name, value))
    _safe_output_require(not frame)
    return tuple(payloads)


def _safe_response_json(
    response: _JsonResponse,
    database: DatabaseSettings,
) -> Mapping[str, object]:
    try:
        untrusted_body = response.json()
    except (json.JSONDecodeError, ValueError):
        raise AssertionError(_SAFE_OUTPUT_ERROR) from None
    _assert_safe_tree(untrusted_body)
    _assert_serialized_output_has_no_secrets_or_sql(response.text, database)
    _safe_output_require(isinstance(untrusted_body, dict))
    return cast(Mapping[str, object], untrusted_body)


def _safe_output_require(condition: bool) -> None:
    if not condition:
        raise AssertionError(_SAFE_OUTPUT_ERROR)


def _is_forbidden_field_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = _normalize_sensitive_label(value)
    return (
        normalized in _FORBIDDEN_KEYS
        or normalized.endswith("_sql")
        or normalized.startswith("sql_")
        or _has_credential_label_suffix(normalized)
    )


def _normalize_sensitive_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = normalized.casefold()
    return re.sub(r"[\s./\\_-]+", "_", normalized).strip("_")


def _is_credential_label(value: str) -> bool:
    normalized = _normalize_sensitive_label(value)
    compact = normalized.replace("_", "")
    parts = tuple(part for part in normalized.split("_") if part)
    if normalized in {
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }:
        return True
    if compact in {"apikey", "clientsecret", "databasepassword", "accesstoken"}:
        return True
    return (
        len(parts) >= 2
        and parts[-2:]
        in {
            ("api", "key"),
            ("client", "secret"),
            ("database", "password"),
            ("access", "token"),
        }
    )


def _has_credential_label_suffix(value: str) -> bool:
    normalized = _normalize_sensitive_label(value)
    parts = tuple(part for part in normalized.split("_") if part)
    if parts and parts[-1] in {
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }:
        return True
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    return any(
        compact.endswith(label)
        for label in ("apikey", "clientsecret", "databasepassword", "accesstoken")
    )


def _contains_credential_assignment(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    if re.search(r"(?i)(?<![\w-])bearer\s+\S+", normalized):
        return True
    for delimiter in re.finditer(r"[:=]", normalized):
        if _has_credential_label_suffix(normalized[: delimiter.start()]):
            return True
    return False


_SQL_QUERY_HEAD = re.compile(r"^(?:select|with|values)\b", re.IGNORECASE)
_SQL_GRANT_HEAD = re.compile(r"^grant\s+", re.IGNORECASE)
_SQL_COPY_HEAD = re.compile(r"^copy\s+", re.IGNORECASE)
_SQL_GRANT_STRUCTURE = re.compile(
    r"^grant\s+(?:"
    r"(?:(?:all(?:\s+privileges)?|select|insert|update|delete|truncate|references|"
    r"trigger|usage|create|connect|temporary|execute|maintain|set|alter\s+system)"
    r"(?:\s*\([^)]*\))?(?:\s*,\s*(?:select|insert|update|delete|truncate|references|"
    r"trigger|usage|create|connect|temporary|execute|maintain|set|alter\s+system)"
    r"(?:\s*\([^)]*\))?)*\s+on\s+"
    r"(?:(?:table|sequence|database|domain|schema|tablespace|type|language|"
    r"function|procedure|routine|large\s+object|foreign\s+server|"
    r"foreign\s+data\s+wrapper)\s+)?\S+(?:\s*,\s*\S+)*)"
    r"|(?:\S+(?:\s*,\s*\S+)*))"
    r"\s+to\s+\S+(?:\s*,\s*\S+)*"
    r"(?:\s+with\s+(?:grant|admin|inherit|set)\s+option)?"
    r"(?:\s+granted\s+by\s+\S+)?$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_COPY_STRUCTURE = re.compile(
    r"^copy\s+(?:\(.+\)|[^\s(]+(?:\s*\([^)]*\))?)\s+"
    r"(?:to|from)\s+(?:stdin|stdout|program\s+\S+|\S+)"
    r"(?:\s+with(?:\s*\([^)]*\)|\s+.+))?"
    r"(?:\s+where\s+.+)?$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_COMMAND_STRUCTURE = re.compile(
    r"^(?:"
    r"insert\s+into\s+\S+|"
    r"update\s+\S+\s+set\b|"
    r"delete\s+from\s+\S+|"
    r"merge\s+into\s+\S+|"
    r"revoke\s+.+\s+(?:on\s+(?:table\s+)?\S+\s+)?from\s+\S+|"
    r"create\s+(?:(?:or\s+replace|temp(?:orary)?|unlogged)\s+)*"
    r"(?:materialized\s+)?"
    r"(?:table|view|index|schema|database|role|function|procedure|type|extension)\b|"
    r"alter\s+(?:table|view|index|schema|database|role|function|procedure|type)\b|"
    r"drop\s+(?:table|view|materialized\s+view|index|schema|database|role|function|"
    r"procedure|type|extension)\b|"
    r"truncate(?:\s+table)?\s+\S+|"
    r"set\s+(?:local\s+|session\s+)?\S+\s*(?:=|to)\s*\S+|"
    r"vacuum(?:\s*\([^)]*\)|\s+\S+)"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def _split_sql_statements(value: str) -> tuple[str, ...]:
    tokens = Tokenizer(dialect="postgres").tokenize(value)
    statements: list[str] = []
    start = 0
    for token in tokens:
        if token.token_type != TokenType.SEMICOLON:
            continue
        statements.append(value[start : token.start])
        start = token.end + 1
    statements.append(value[start:])
    return tuple(statements)


def _is_single_sql_statement(value: str) -> bool:
    normalized = _normalize_sql_text(value).strip()
    if not normalized:
        return False
    if _SQL_GRANT_STRUCTURE.fullmatch(normalized):
        return True
    if _SQL_GRANT_HEAD.match(normalized):
        return bool(re.search(r"\bon\b.+\bto\b", normalized, re.IGNORECASE | re.DOTALL))
    if _SQL_COPY_STRUCTURE.fullmatch(normalized):
        return True
    if _SQL_COPY_HEAD.match(normalized):
        return bool(
            re.search(r"\b(?:to|from)\b", normalized, re.IGNORECASE | re.DOTALL)
        )
    if _SQL_COMMAND_STRUCTURE.match(normalized):
        return True
    if not _SQL_QUERY_HEAD.match(normalized):
        return False
    try:
        validate_sql(value)
    except SqlPolicyError as failure:
        return failure.code not in {
            SqlRejectionCode.EMPTY_SQL,
            SqlRejectionCode.INVALID_SQL,
        }
    return True


def _is_sql_statement(value: str) -> bool:
    try:
        statements = _split_sql_statements(value)
    except TokenError:
        normalized = _normalize_sql_text(value).strip()
        return bool(
            _SQL_QUERY_HEAD.match(normalized)
            or _SQL_GRANT_HEAD.match(normalized)
            or _SQL_COPY_HEAD.match(normalized)
            or _SQL_COMMAND_STRUCTURE.match(normalized)
        )
    return any(_is_single_sql_statement(statement) for statement in statements)


def _assert_safe_string(value: str) -> None:
    _safe_output_require(not _is_credential_label(value))
    _safe_output_require(not _contains_credential_assignment(value))
    _safe_output_require(not _is_sql_statement(value))


def _assert_safe_tree(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, str):
        _assert_safe_string(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _safe_output_require(isinstance(key, str))
            _safe_output_require(not _is_forbidden_field_name(key))
            key = cast(str, key)
            _assert_safe_string(key)
            normalized_key = unicodedata.normalize("NFKC", key).strip().casefold()
            _assert_safe_tree(item, path=(*path, normalized_key))
    elif isinstance(value, (list, tuple)):
        if path and path[-1] == "safe_arguments":
            for pair in value:
                _safe_output_require(
                    isinstance(pair, (list, tuple))
                    and len(pair) == 2
                    and isinstance(pair[0], str)
                    and not _is_forbidden_field_name(pair[0])
                )
                typed_pair = cast(list[object] | tuple[object, ...], pair)
                pair_name = cast(str, typed_pair[0])
                _assert_safe_string(pair_name)
                _assert_safe_tree(typed_pair[1], path=(*path, pair_name))
            return
        for item in value:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
            ):
                pair = cast(list[object] | tuple[object, ...], item)
                _safe_output_require(not _is_forbidden_field_name(pair[0]))
                _assert_safe_tree(pair[0], path=(*path, "name"))
                _assert_safe_tree(pair[1], path=(*path, "value"))
            else:
                _assert_safe_tree(item, path=(*path, "*"))


def _assert_exact_keys(value: object, allowed: frozenset[str]) -> Mapping[str, object]:
    _safe_output_require(isinstance(value, dict))
    value = cast(dict[object, object], value)
    _safe_output_require(frozenset(map(str, value)) == allowed)
    return cast(Mapping[str, object], value)


def _assert_wire_allowlists(
    created: object,
    events: tuple[dict[str, object], ...],
    status: object,
    trace: object,
) -> None:
    _assert_exact_keys(created, _CREATE_KEYS)
    for event in events:
        envelope = _assert_exact_keys(event, _EVENT_KEYS)
        event_type = envelope["type"]
        _safe_output_require(isinstance(event_type, str))
        event_type = cast(str, event_type)
        _safe_output_require(event_type in _EVENT_DATA_KEYS)
        _assert_exact_keys(envelope["data"], _EVENT_DATA_KEYS[event_type])

    status_mapping = _assert_exact_keys(status, _STATUS_KEYS)
    evidence = status_mapping["evidence"]
    _safe_output_require(isinstance(evidence, list))
    for item in cast(list[object], evidence):
        _assert_exact_keys(item, _EVIDENCE_KEYS)

    trace_mapping = _assert_exact_keys(trace, _TRACE_KEYS)
    nodes = trace_mapping["nodes"]
    model_calls = trace_mapping["model_calls"]
    tool_calls = trace_mapping["tool_calls"]
    _safe_output_require(isinstance(nodes, list))
    _safe_output_require(isinstance(model_calls, list))
    _safe_output_require(isinstance(tool_calls, list))
    for item in cast(list[object], nodes):
        _assert_exact_keys(item, _NODE_TRACE_KEYS)
    observed_model_purposes: list[object] = []
    for item in cast(list[object], model_calls):
        model_trace = _assert_exact_keys(item, _MODEL_TRACE_KEYS)
        observed_model_purposes.append(model_trace["purpose"])
        _safe_output_require(isinstance(model_trace["provider_model"], str))
        _safe_output_require(model_trace["outcome"] == "completed")
        _safe_output_require(model_trace["safe_error"] is None)
        for counter_name in ("latency_ms", "input_tokens", "output_tokens"):
            counter = model_trace[counter_name]
            _safe_output_require(isinstance(counter, int) and counter >= 0)
        _safe_output_require(isinstance(model_trace["output_truncated"], bool))
    _safe_output_require(
        tuple(observed_model_purposes)
        == ("behavior", "plan", "action", "action", "action", "action", "synthesis")
    )
    observed_tools: list[tuple[object, object, tuple[tuple[object, object], ...]]] = []
    for item in cast(list[object], tool_calls):
        tool_trace = _assert_exact_keys(item, _TOOL_TRACE_KEYS)
        tool_name = tool_trace["tool_name"]
        safe_arguments = tool_trace["safe_arguments"]
        _safe_output_require(
            isinstance(tool_name, str) and tool_name in _SAFE_ARGUMENT_NAMES_BY_TOOL
        )
        _safe_output_require(isinstance(safe_arguments, list))
        tool_name = cast(str, tool_name)
        safe_arguments = cast(list[object], safe_arguments)
        query_id = tool_trace["query_id"]
        columns = tool_trace["columns"]
        row_count = tool_trace["row_count"]
        _safe_output_require(isinstance(columns, list))
        _safe_output_require(
            all(isinstance(column, str) and column for column in cast(list[object], columns))
        )
        _safe_output_require(isinstance(tool_trace["possibly_truncated"], bool))
        _safe_output_require(tool_trace["safe_error"] is None)
        if tool_name == "execute_sql":
            _safe_output_require(isinstance(query_id, str) and len(query_id) == 64)
            _safe_output_require(isinstance(row_count, int) and row_count >= 0)
        else:
            _safe_output_require(query_id is None)
            _safe_output_require(row_count is None)
        allowed_names = _SAFE_ARGUMENT_NAMES_BY_TOOL[tool_name]
        observed_names: list[str] = []
        observed_arguments: list[tuple[object, object]] = []
        for pair in safe_arguments:
            _safe_output_require(
                isinstance(pair, list)
                and len(pair) == 2
                and isinstance(pair[0], str)
                and pair[0] in allowed_names
            )
            pair = cast(list[object], pair)
            pair_name = cast(str, pair[0])
            observed_names.append(pair_name)
            observed_arguments.append((pair_name, pair[1]))
            _assert_safe_tree(
                {"safe_arguments": [pair]},
            )
        _safe_output_require(len(observed_names) == len(set(observed_names)))
        observed_tools.append((tool_name, tool_trace["purpose"], tuple(observed_arguments)))
    expected_tools = (
        ("metric_lookup", "metric_lookup", ()),
        ("schema_lookup", "schema_lookup", ()),
        (
            "execute_sql",
            "gmv_comparison",
            (("contract_id", "gmv_comparison"), ("hypothesis_id", "confirm_decline")),
        ),
        (
            "execute_sql",
            "region_contribution",
            (
                ("contract_id", "region_contribution"),
                ("hypothesis_id", "region_contribution"),
            ),
        ),
        (
            "execute_sql",
            "sku_contribution",
            (("contract_id", "sku_contribution"), ("hypothesis_id", "sku_contribution")),
        ),
        (
            "execute_sql",
            "segment_contribution",
            (
                ("contract_id", "segment_contribution"),
                ("hypothesis_id", "segment_contribution"),
            ),
        ),
    )
    _safe_output_require(tuple(observed_tools) == expected_tools)


def _assert_serialized_output_has_no_secrets_or_sql(
    wire_text: str,
    database: DatabaseSettings,
) -> None:
    parsed_database_url = urlsplit(database.database_url)
    username = unquote(parsed_database_url.username or "")
    password = unquote(parsed_database_url.password or "")
    _safe_output_require(bool(username and password))
    secrets = (
        database.database_url,
        username,
        password,
        _CREDENTIAL_CANARY,
    )
    _safe_output_require(not any(secret in wire_text for secret in secrets))

    try:
        wire_value = json.loads(wire_text)
    except json.JSONDecodeError:
        wire_value = wire_text
    _assert_safe_tree(wire_value)
    string_values = tuple(_string_leaves(wire_value))
    normalized_values = tuple(_normalize_sql_text(value) for value in string_values)
    normalized_sql_canaries = tuple(
        _normalize_sql_text(sql) for sql in (_COMPARISON_SQL, _REGION_SQL, _SKU_SQL, _SEGMENT_SQL)
    )
    _safe_output_require(
        not any(
            sql_canary in value
            for sql_canary in normalized_sql_canaries
            for value in normalized_values
        )
    )
    partial_signatures = (
        frozenset({"current_gmv", "previous_gmv", "change_rate", "order_items"}),
        frozenset({"gmv_loss", "order_items", "region", "previous_start"}),
        frozenset({"gmv_loss", "order_items", "products", "sku"}),
        frozenset({"segment_gmv", "customers", "previous_gmv", "current_gmv"}),
    )
    _safe_output_require(
        not any(
            all(token in value for token in signature)
            for value in normalized_values
            for signature in partial_signatures
        )
    )


def _normalize_sql_text(value: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/", " ", value, flags=re.DOTALL)
    without_comments = re.sub(r"--[^\r\n]*", " ", without_comments)
    return " ".join(without_comments.casefold().split())


def _string_leaves(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)


def _assert_failure_is_redacted(
    failure: pytest.ExceptionInfo[AssertionError],
    sensitive_operands: tuple[str, ...],
) -> None:
    message = str(failure.value)
    safe_trace = "".join(traceback.format_exception(failure.type, failure.value, failure.tb))
    if message != _SAFE_OUTPUT_ERROR or any(
        operand in message or operand in safe_trace for operand in sensitive_operands
    ):
        pytest.fail("safe-output failure diagnostics leaked operands", pytrace=False)


def _safe_created_event(*, status: str = "queued") -> dict[str, object]:
    return {
        "event_id": "safe-event-1",
        "sequence": 1,
        "run_id": "safe-run",
        "timestamp": "2026-09-05T00:00:00Z",
        "node": "runtime",
        "type": "run.created",
        "data": {"status": status},
    }


def _oracle_database() -> DatabaseSettings:
    return DatabaseSettings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=(
            "postgresql+asyncpg://analytics_readonly:oracle_password@localhost/oracle_db"
        ),
    )


def test_sse_parser_accepts_real_crlf_multiline_data_and_rejects_malformed_frames() -> None:
    database = _oracle_database()
    safe_event = _safe_created_event()
    encoded = ServerSentEvent(
        data=json.dumps(safe_event, indent=2),
        event="run.created",
        id="safe-event-1",
        sep="\r\n",
    ).encode()
    real_lines = tuple(encoded.decode("utf-8").splitlines())
    _safe_output_require(sum(line.startswith("data:") for line in real_lines) > 1)
    _safe_output_require(_sse_payloads(iter(real_lines), database) == (safe_event,))

    no_space_frame = (
        "",
        ":heartbeat",
        "",
        "id:safe-event-1",
        "event:run.created",
        f"data:{json.dumps(safe_event)}",
        "",
        "",
    )
    _safe_output_require(_sse_payloads(iter(no_space_frame), database) == (safe_event,))

    malformed_frames = (
        (
            "id: safe-event-1",
            "id: duplicate-event-id",
            "event: run.created",
            f"data: {json.dumps(safe_event)}",
            "",
        ),
        (
            "id: safe-event-1",
            "event: run.created",
            "event: run.created",
            f"data: {json.dumps(safe_event)}",
            "",
        ),
        ("id: safe-event-1", f"data: {json.dumps(safe_event)}", ""),
        ("id: safe-event-1", "event: run.created", ""),
        (
            "id: safe-event-1",
            "event: run.started",
            f"data: {json.dumps(safe_event)}",
            "",
        ),
    )
    for malformed_frame in malformed_frames:
        with pytest.raises(AssertionError) as frame_failure:
            _sse_payloads(iter(malformed_frame), database)
        _assert_failure_is_redacted(
            frame_failure,
            tuple(line for line in malformed_frame if line),
        )


def test_sse_safe_output_oracle_scans_parsed_string_leaves() -> None:
    database = _oracle_database()
    unsafe_statuses = (
        "token=credential-canary",
        "secret=credential-canary",
        "credentials=credential-canary",
        "DELETE FROM audit_log WHERE true",
        "SELECT account_id FROM audit_log",
        "WITH exposed AS (SELECT 1) SELECT * FROM exposed",
        "INSERT INTO audit_log(message) VALUES ('exposed')",
        "UPDATE audit_log SET message = 'exposed'",
        "CREATE TABLE exposed_audit_log(id integer)",
        "ALTER TABLE audit_log ADD COLUMN exposed integer",
        "DROP TABLE audit_log",
        "TRUNCATE TABLE audit_log",
    )
    for unsafe_status in unsafe_statuses:
        unsafe_event = _safe_created_event(status=unsafe_status)
        unsafe_frame = (
            "id: safe-event-1",
            "event: run.created",
            f"data: {json.dumps(unsafe_event)}",
            "",
        )
        with pytest.raises(AssertionError) as unsafe_failure:
            _sse_payloads(iter(unsafe_frame), database)
        _assert_failure_is_redacted(
            unsafe_failure,
            (unsafe_status, json.dumps(unsafe_event), *unsafe_frame[:-1]),
        )

    natural_language = _safe_created_event(status="select the best evidence")
    natural_language_frame = (
        "id: safe-event-1",
        "event: run.created",
        f"data: {json.dumps(natural_language)}",
        "",
    )
    _safe_output_require(
        _sse_payloads(iter(natural_language_frame), database) == (natural_language,)
    )


def test_safe_output_oracle_scans_every_structured_and_raw_sse_surface(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _oracle_database()
    credential_labels = (
        "APIKey",
        "APIKEY",
        "OPENAIAPIKEY",
        "providerAPIKey",
        "provider.api_key",
        "apiKey",
        "api key",
        "api/key",
        "\uff41\uff50\uff49Key",
        "\uff21\uff30\uff29\uff2b\uff25\uff39",
        "CLIENTSECRET",
        "client_secret",
        "DATABASEPASSWORD",
        "database.password",
        "ACCESSTOKEN",
        "access-token",
        "authorization",
        "bearer",
        "credentials",
    )
    for index, label in enumerate(credential_labels):
        opaque_value = f"opaque-sensitive-value-{index}"
        candidates: tuple[object, ...] = (
            {label: opaque_value},
            {"status": f"{label}={opaque_value}"},
            [[label, opaque_value]],
            [["status", f"{label}={opaque_value}"]],
            {"safe_arguments": [[label, opaque_value]]},
            {"safe_arguments": [["contract_id", f"{label}={opaque_value}"]]},
        )
        for candidate in candidates:
            serialized = json.dumps(candidate)
            with pytest.raises(AssertionError) as failure:
                _assert_serialized_output_has_no_secrets_or_sql(serialized, database)
            _assert_failure_is_redacted(failure, (label, opaque_value, serialized))
        raw_comment = f": {label}={opaque_value}"
        with pytest.raises(AssertionError) as raw_failure:
            _sse_payloads(iter((raw_comment, "")), database)
        _assert_failure_is_redacted(raw_failure, (label, opaque_value, raw_comment))

    unicode_assignments = (
        "\uff41\uff50\uff49Key\uff1dopaque-fullwidth-equals",
        "clientSecret\uff1aopaque-fullwidth-colon",
    )
    for assignment in unicode_assignments:
        serialized = json.dumps({"status": assignment})
        with pytest.raises(AssertionError) as failure:
            _assert_serialized_output_has_no_secrets_or_sql(serialized, database)
        _assert_failure_is_redacted(failure, (assignment, serialized))
        raw_comment = f": {assignment}"
        with pytest.raises(AssertionError) as raw_failure:
            _sse_payloads(iter((raw_comment, "")), database)
        _assert_failure_is_redacted(raw_failure, (assignment, raw_comment))

    prefixed_assignments = (
        "Use APIKey=opaque-prefixed-api-key",
        "please use token=opaque-prefixed-token",
        "OPENAIAPIKEY=opaque-openai-key",
        "providerAPIKey=opaque-provider-key",
        "provider.api_key=opaque-provider-dot-key",
        "\uff2f\uff30\uff25\uff2e\uff21\uff29\uff21\uff30\uff29\uff2b\uff25\uff39"
        "\uff1dopaque-fullwidth-provider-key",
    )
    for assignment in prefixed_assignments:
        candidates = (
            {"status": assignment},
            [["status", assignment]],
            {"safe_arguments": [["contract_id", assignment]]},
        )
        for candidate in candidates:
            serialized = json.dumps(candidate)
            with pytest.raises(AssertionError) as failure:
                _assert_serialized_output_has_no_secrets_or_sql(serialized, database)
            _assert_failure_is_redacted(failure, (assignment, serialized))
        raw_comment = f": {assignment}"
        with pytest.raises(AssertionError) as raw_failure:
            _sse_payloads(iter((raw_comment, "")), database)
        _assert_failure_is_redacted(raw_failure, (assignment, raw_comment))

    sql_categories = (
        (
            "query",
            (
                "SELECT 1",
                "SELECT ';' AS marker",
                "SELECT 1 /* audit; marker */",
                "SELECT 1; -- trailing comment",
                "VALUES (1)",
            ),
        ),
        (
            "cte",
            (
                "WITH exposed AS (SELECT 1) SELECT * FROM exposed",
                "WITH exposed(value) AS (VALUES (1)) SELECT value FROM exposed",
                "WITH marker AS (SELECT ';' AS value) SELECT value FROM marker",
            ),
        ),
        (
            "dml",
            (
                "DELETE FROM audit_log",
                "UPDATE audit_log SET message = 'unsafe'",
                "INSERT INTO audit_log(message) VALUES ('unsafe')",
            ),
        ),
        (
            "ddl",
            (
                "CREATE TABLE unsafe_log(id integer)",
                "CREATE TEMP TABLE unsafe_log(id integer)",
                "ALTER TABLE audit_log ADD COLUMN unsafe integer",
                "DROP TABLE audit_log",
                "TRUNCATE TABLE audit_log",
            ),
        ),
        (
            "grant",
            (
                "GRANT SELECT ON orders TO analyst",
                "GRANT ALL PRIVILEGES ON TABLE orders TO analyst",
                "GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst",
                "GRANT SELECT, UPDATE ON TABLE orders TO analyst WITH GRANT OPTION",
                "GRANT analyst_role TO report_user WITH ADMIN OPTION;",
                "GRANT SELECT ON orders TO analyst; /* trailing; comment */",
                "GRANT SELECT ON orders TO analyst; DROP TABLE audit_log",
            ),
        ),
        (
            "copy",
            (
                "COPY orders TO STDOUT",
                "COPY orders (order_id) TO STDOUT",
                "COPY (SELECT * FROM orders) TO STDOUT",
                "COPY (SELECT ';' AS marker FROM orders) TO STDOUT",
                "COPY orders TO STDOUT; COPY audit_log TO STDOUT",
            ),
        ),
    )
    for category_index, (_category, statements) in enumerate(sql_categories):
        for statement in statements:
            candidates = (
                {statement: f"safe-{category_index}"},
                {"status": statement},
                [["status", statement]],
                {"safe_arguments": [["contract_id", statement]]},
            )
            for candidate in candidates:
                serialized = json.dumps(candidate)
                with pytest.raises(AssertionError) as failure:
                    _assert_serialized_output_has_no_secrets_or_sql(serialized, database)
                _assert_failure_is_redacted(failure, (statement, serialized))
            raw_comment = f": {statement}"
            with pytest.raises(AssertionError) as raw_failure:
                _sse_payloads(iter((raw_comment, "")), database)
            _assert_failure_is_redacted(raw_failure, (statement, raw_comment))

    unsafe_raw_lines = (
        ": apiKey=opaque-comment-value",
        "client_secret: opaque-field-name-value",
        "id: database.password=opaque-field-value",
        "data: SELECT 1",
    )
    for line in unsafe_raw_lines:
        with pytest.raises(AssertionError) as failure:
            _sse_payloads(iter((line, "")), database)
        _assert_failure_is_redacted(failure, (line,))

    safe_prose = (
        "please select a cached model from the registry for this explanation",
        "with cached model metadata we can explain the selection",
        "drop shipping is selected from cache",
        "grant access to the cached model",
        "grant access to analyst in the cached model",
        "grant-model",
        "sentinel-llm",
    )
    for prose in safe_prose:
        _assert_serialized_output_has_no_secrets_or_sql(
            json.dumps({"status": prose}),
            database,
        )
    captured = capsys.readouterr()
    _safe_output_require(captured.out == "" and captured.err == "")
    _safe_output_require(
        not any(record.name.startswith("sqlglot") for record in caplog.records)
    )


async def _late_success_after_cancellation(
    started: asyncio.Event,
    release: asyncio.Event,
    finished: asyncio.Event,
) -> str:
    started.set()
    try:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
    finally:
        finished.set()
    return "late-success"


@pytest.mark.asyncio
async def test_await_deadline_rejects_late_success_and_reclaims_hostile_task() -> None:
    deadline_seconds = 0.01
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    hostile = asyncio.create_task(
        _late_success_after_cancellation(started, release, finished),
        name="sse-hostile-late-success",
    )
    await started.wait()
    loop = asyncio.get_running_loop()
    release_handle = loop.call_later(deadline_seconds * 5, release.set)
    started_at = loop.time()
    try:
        with pytest.raises(AssertionError) as deadline_failure:
            await _await_with_deadline(
                hostile,
                seconds=deadline_seconds,
                failure_message=_SSE_DEADLINE_ERROR,
            )
        elapsed = loop.time() - started_at
        _safe_output_require(str(deadline_failure.value) == _SSE_DEADLINE_ERROR)
        _safe_output_require(elapsed < deadline_seconds * 4)
        _safe_output_require(not hostile.done())
    finally:
        release_handle.cancel()
        release.set()
        await asyncio.gather(hostile, return_exceptions=True)
    _safe_output_require(finished.is_set())
    _safe_output_require(hostile.done())


@pytest.mark.asyncio
async def test_close_deadline_uses_one_budget_and_reclaims_hostile_task() -> None:
    deadline_seconds = 0.04
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    hostile = asyncio.create_task(
        _late_success_after_cancellation(started, release, finished),
        name="sse-hostile-close",
    )
    await started.wait()
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    try:
        with pytest.raises(AssertionError) as cleanup_failure:
            await _close_with_deadline(lambda: hostile, seconds=deadline_seconds)
        elapsed = loop.time() - started_at
        _safe_output_require(str(cleanup_failure.value) == _CLEANUP_DEADLINE_ERROR)
        _safe_output_require(elapsed < deadline_seconds * 2.25)
        _safe_output_require(not hostile.done())
    finally:
        release.set()
        await asyncio.gather(hostile, return_exceptions=True)
    _safe_output_require(finished.is_set())
    _safe_output_require(hostile.done())


@pytest.mark.asyncio
async def test_await_deadline_owns_coroutines_across_all_completion_paths() -> None:
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)

    async def return_value() -> str:
        return "complete"

    async def raise_child_error() -> None:
        raise RuntimeError("owned-child-error")

    async def raise_intrinsic_timeout() -> None:
        raise TimeoutError("owned-intrinsic-timeout")

    _safe_output_require(
        await _await_with_deadline(
            return_value(), seconds=1.0, failure_message=_SSE_DEADLINE_ERROR
        )
        == "complete"
    )
    with pytest.raises(RuntimeError, match="owned-child-error"):
        await _await_with_deadline(
            raise_child_error(), seconds=1.0, failure_message=_SSE_DEADLINE_ERROR
        )
    with pytest.raises(TimeoutError, match="owned-intrinsic-timeout"):
        await _await_with_deadline(
            raise_intrinsic_timeout(), seconds=1.0, failure_message=_SSE_DEADLINE_ERROR
        )

    deadline_started = asyncio.Event()
    deadline_cancelled = asyncio.Event()

    async def block_until_deadline() -> None:
        deadline_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            deadline_cancelled.set()

    with pytest.raises(AssertionError) as deadline_failure:
        await _await_with_deadline(
            block_until_deadline(), seconds=0.01, failure_message=_SSE_DEADLINE_ERROR
        )
    _safe_output_require(str(deadline_failure.value) == _SSE_DEADLINE_ERROR)
    _safe_output_require(deadline_started.is_set())
    await asyncio.wait_for(deadline_cancelled.wait(), timeout=1.0)

    caller_started = asyncio.Event()
    caller_cancelled = asyncio.Event()

    async def block_until_caller_cancel() -> None:
        caller_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            caller_cancelled.set()

    caller = asyncio.create_task(
        _await_with_deadline(
            block_until_caller_cancel(),
            seconds=1.0,
            failure_message=_SSE_DEADLINE_ERROR,
        ),
        name="owned-deadline-caller",
    )
    await caller_started.wait()
    caller.cancel()
    cancelled_result = await asyncio.gather(caller, return_exceptions=True)
    _safe_output_require(
        len(cancelled_result) == 1
        and isinstance(cancelled_result[0], asyncio.CancelledError)
    )
    await asyncio.wait_for(caller_cancelled.wait(), timeout=1.0)
    await asyncio.sleep(0)
    _safe_output_require(_pending_tasks(current) == pending_before)


@pytest.mark.asyncio
async def test_await_deadline_exposes_and_reclaims_owned_hostile_coroutine() -> None:
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    handle = _OwnedTaskHandle()

    with pytest.raises(AssertionError) as deadline_failure:
        await _await_with_deadline(
            _late_success_after_cancellation(started, release, finished),
            seconds=0.01,
            failure_message=_SSE_DEADLINE_ERROR,
            owned_task_handle=handle,
        )
    _safe_output_require(str(deadline_failure.value) == _SSE_DEADLINE_ERROR)
    _safe_output_require(started.is_set())
    owned_task = handle.task
    _safe_output_require(owned_task is not None)
    assert owned_task is not None
    _safe_output_require(not owned_task.done())
    _safe_output_require(owned_task.get_name() == "sse-test-deadline")
    _safe_output_require(not finished.is_set())

    release.set()
    await handle.reclaim()
    _safe_output_require(owned_task.done())
    _safe_output_require(finished.is_set())
    _safe_output_require(_pending_tasks(current) == pending_before)


@pytest.mark.asyncio
async def test_owned_handle_reuse_rejects_before_task_creation_and_closes_coroutine() -> None:
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)
    handle = _OwnedTaskHandle()

    async def complete_once() -> str:
        return "complete"

    _safe_output_require(
        await _await_with_deadline(
            complete_once(),
            seconds=1.0,
            failure_message=_SSE_DEADLINE_ERROR,
            owned_task_handle=handle,
        )
        == "complete"
    )
    bound_task = handle.task
    _safe_output_require(bound_task is not None and bound_task.done())

    started = False
    release = asyncio.Event()

    async def rejected_operation() -> None:
        nonlocal started
        started = True
        await release.wait()

    rejected_coroutine = rejected_operation()
    introduced: frozenset[asyncio.Task[object]] = frozenset()
    try:
        with pytest.raises(AssertionError) as reuse_failure:
            await _await_with_deadline(
                rejected_coroutine,
                seconds=1.0,
                failure_message=_SSE_DEADLINE_ERROR,
                owned_task_handle=handle,
            )
        _safe_output_require(str(reuse_failure.value) == _SAFE_OUTPUT_ERROR)
        await asyncio.sleep(0)
        introduced = _pending_tasks(current) - pending_before
        _safe_output_require(introduced == frozenset())
        _safe_output_require(not started)
        _safe_output_require(
            inspect.getcoroutinestate(rejected_coroutine) == inspect.CORO_CLOSED
        )
    finally:
        release.set()
        if introduced:
            await asyncio.gather(*introduced, return_exceptions=True)
    _safe_output_require(_pending_tasks(current) == pending_before)


@pytest.mark.asyncio
async def test_borrowed_future_rejects_owned_handle_without_ownership() -> None:
    loop = asyncio.get_running_loop()
    borrowed: asyncio.Future[str] = loop.create_future()
    handle = _OwnedTaskHandle()
    with pytest.raises(AssertionError) as handle_failure:
        await _await_with_deadline(
            borrowed,
            seconds=1.0,
            failure_message=_SSE_DEADLINE_ERROR,
            owned_task_handle=handle,
        )
    _safe_output_require(str(handle_failure.value) == _SAFE_OUTPUT_ERROR)
    _safe_output_require(handle.task is None)
    _safe_output_require(not borrowed.done() and not borrowed.cancelled())
    borrowed.set_result("released-by-owner")
    _safe_output_require(await borrowed == "released-by-owner")


@pytest.mark.asyncio
async def test_await_deadline_never_takes_ownership_of_borrowed_task_or_future() -> None:
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)

    normal = asyncio.create_task(asyncio.sleep(0, result="complete"), name="borrowed-normal")
    _safe_output_require(
        await _await_with_deadline(
            normal, seconds=1.0, failure_message=_SSE_DEADLINE_ERROR
        )
        == "complete"
    )
    _safe_output_require(normal.get_name() == "borrowed-normal" and not normal.cancelled())

    async def raise_child_error() -> None:
        raise RuntimeError("borrowed-child-error")

    exceptional = asyncio.create_task(raise_child_error(), name="borrowed-exception")
    with pytest.raises(RuntimeError, match="borrowed-child-error"):
        await _await_with_deadline(
            exceptional, seconds=1.0, failure_message=_SSE_DEADLINE_ERROR
        )
    _safe_output_require(
        exceptional.get_name() == "borrowed-exception" and not exceptional.cancelled()
    )

    intrinsic_timeout = asyncio.create_task(
        _raise_operation_timeout(), name="borrowed-intrinsic-timeout"
    )
    with pytest.raises(TimeoutError, match="operation-owned-timeout"):
        await _await_with_deadline(
            intrinsic_timeout, seconds=1.0, failure_message=_SSE_DEADLINE_ERROR
        )
    _safe_output_require(
        intrinsic_timeout.get_name() == "borrowed-intrinsic-timeout"
        and not intrinsic_timeout.cancelled()
    )

    deadline_release = asyncio.Event()
    deadline_borrowed = asyncio.create_task(
        deadline_release.wait(), name="borrowed-real-deadline"
    )
    try:
        with pytest.raises(AssertionError) as deadline_failure:
            await _await_with_deadline(
                deadline_borrowed,
                seconds=0.01,
                failure_message=_SSE_DEADLINE_ERROR,
            )
        _safe_output_require(str(deadline_failure.value) == _SSE_DEADLINE_ERROR)
        _safe_output_require(
            deadline_borrowed.get_name() == "borrowed-real-deadline"
            and not deadline_borrowed.done()
            and not deadline_borrowed.cancelled()
        )
    finally:
        deadline_release.set()
        await deadline_borrowed

    cancellation_release = asyncio.Event()
    cancellation_borrowed = asyncio.create_task(
        cancellation_release.wait(), name="borrowed-caller-cancellation"
    )
    borrower = asyncio.create_task(
        _await_with_deadline(
            cancellation_borrowed,
            seconds=1.0,
            failure_message=_SSE_DEADLINE_ERROR,
        ),
        name="borrowed-await-caller",
    )
    await asyncio.sleep(0)
    borrower.cancel()
    borrower_result = await asyncio.gather(borrower, return_exceptions=True)
    _safe_output_require(
        len(borrower_result) == 1
        and isinstance(borrower_result[0], asyncio.CancelledError)
    )
    _safe_output_require(
        cancellation_borrowed.get_name() == "borrowed-caller-cancellation"
        and not cancellation_borrowed.done()
        and not cancellation_borrowed.cancelled()
    )
    cancellation_release.set()
    await cancellation_borrowed

    close_borrowed = asyncio.create_task(
        asyncio.sleep(0, result=None), name="borrowed-close-task"
    )
    await _close_with_deadline(lambda: close_borrowed, seconds=1.0)
    _safe_output_require(
        close_borrowed.get_name() == "borrowed-close-task" and not close_borrowed.cancelled()
    )
    await asyncio.sleep(0)
    _safe_output_require(_pending_tasks(current) == pending_before)


@pytest.mark.asyncio
async def test_borrowed_future_survives_real_deadline_and_caller_cancellation() -> None:
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    pending_before = _pending_tasks(current)

    deadline_future: asyncio.Future[str] = loop.create_future()
    with pytest.raises(AssertionError) as deadline_failure:
        await _await_with_deadline(
            deadline_future,
            seconds=0.01,
            failure_message=_SSE_DEADLINE_ERROR,
        )
    _safe_output_require(str(deadline_failure.value) == _SSE_DEADLINE_ERROR)
    _safe_output_require(not deadline_future.done() and not deadline_future.cancelled())
    deadline_future.set_result("released-by-owner")
    _safe_output_require(await deadline_future == "released-by-owner")

    cancellation_future: asyncio.Future[str] = loop.create_future()
    borrower = asyncio.create_task(
        _await_with_deadline(
            cancellation_future,
            seconds=1.0,
            failure_message=_SSE_DEADLINE_ERROR,
        ),
        name="borrowed-future-caller",
    )
    await asyncio.sleep(0)
    borrower.cancel()
    borrower_result = await asyncio.gather(borrower, return_exceptions=True)
    _safe_output_require(
        len(borrower_result) == 1
        and isinstance(borrower_result[0], asyncio.CancelledError)
    )
    _safe_output_require(not cancellation_future.done() and not cancellation_future.cancelled())
    cancellation_future.set_result("released-by-owner")
    _safe_output_require(await cancellation_future == "released-by-owner")
    _safe_output_require(not hasattr(deadline_future, "set_name"))
    _safe_output_require(not hasattr(cancellation_future, "set_name"))
    await asyncio.sleep(0)
    _safe_output_require(_pending_tasks(current) == pending_before)


@pytest.mark.asyncio
async def test_caller_cancellation_observes_same_tick_child_exception() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unexpected_contexts: list[dict[str, object]] = []

    def capture_exception(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        unexpected_contexts.append(context)

    async def race() -> None:
        child: asyncio.Future[None] = loop.create_future()
        current = asyncio.current_task()
        assert current is not None
        loop.call_soon(child.set_exception, RuntimeError("same-tick-child-error"))
        loop.call_soon(current.cancel)
        await _await_with_deadline(
            child,
            seconds=1.0,
            failure_message=_SSE_DEADLINE_ERROR,
        )

    loop.set_exception_handler(capture_exception)
    try:
        racing_task = asyncio.create_task(race(), name="same-tick-cancellation-race")
        race_result = await asyncio.gather(racing_task, return_exceptions=True)
        _safe_output_require(
            len(race_result) == 1 and isinstance(race_result[0], asyncio.CancelledError)
        )
        del racing_task
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        _safe_output_require(unexpected_contexts == [])
    finally:
        loop.set_exception_handler(previous_handler)
    _safe_output_require(loop.get_exception_handler() is previous_handler)


@pytest.mark.asyncio
async def test_tracked_close_probe_requires_observed_runtime_error_and_releases_lease() -> None:
    failure_result = await _tracked_close_failure_probe()
    _safe_output_require(failure_result.runtime_error_observed is True)
    _safe_output_require(failure_result.active_leases == 0)

    success_result = await _tracked_close_success_probe()
    _safe_output_require(success_result.runtime_error_observed is False)
    _safe_output_require(success_result.active_leases == 0)


@pytest.mark.integration
def test_post_sse_status_and_trace_share_one_lifespan_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sql_candidate = {"raw_sql": "WITH\nsecret_cte AS (VALUES (1))"}
    with pytest.raises(AssertionError) as raw_sql_failure:
        _assert_safe_tree(raw_sql_candidate)
    _assert_failure_is_redacted(
        raw_sql_failure,
        ("secret_cte", "WITH\nsecret_cte AS (VALUES (1))"),
    )
    _assert_safe_tree({"result_summary": "contracted aggregate only"})
    _assert_safe_tree({"query_id": "a" * 64})
    _assert_safe_tree(
        {
            "dimensions": [["region", "verified dimension value"]],
            "limitations": ["completed with limitations", "select the best evidence"],
        }
    )

    database = DatabaseSettings()  # type: ignore[call-arg]
    safe_event = {
        "event_id": "safe-event-1",
        "sequence": 1,
        "run_id": "safe-run",
        "timestamp": "2026-09-05T00:00:00Z",
        "node": "runtime",
        "type": "run.created",
        "data": {"status": "queued"},
    }
    safe_frame = (
        ": safe heartbeat",
        "id: safe-event-1",
        "event: run.created",
        f"data: {json.dumps(safe_event)}",
        "",
    )
    _safe_output_require(_sse_payloads(iter(safe_frame), database) == (safe_event,))

    sql_line_canary = " ".join(_SEGMENT_SQL.split())
    raw_line_attacks = (
        (
            ": authorization: Bearer comment-credential-canary",
            ("authorization", "comment-credential-canary"),
        ),
        (
            "event: api_key=event-credential-canary",
            ("api_key", "event-credential-canary"),
        ),
        (
            "retry: password=retry-credential-canary",
            ("password", "retry-credential-canary"),
        ),
        (
            f"id: {sql_line_canary}",
            (sql_line_canary, "segment_gmv"),
        ),
    )
    for unsafe_line, raw_canaries in raw_line_attacks:
        unsafe_wire = f"{unsafe_line}\n\n"
        with pytest.raises(AssertionError) as raw_line_failure:
            _sse_payloads(iter((unsafe_line, "")), database)
        _assert_failure_is_redacted(
            raw_line_failure,
            (unsafe_line, unsafe_wire, *raw_canaries),
        )

    second_event = dict(safe_event, sequence=2)
    invalid_frames = (
        (
            "event: run.created",
            f"data: {json.dumps(safe_event)}",
            "",
        ),
        (*safe_frame, *safe_frame),
        (
            "id: safe-event-2",
            "event: run.created",
            f"data: {json.dumps(second_event)}",
            "",
        ),
        (
            "id: safe-event-2",
            "event: run.created",
            f"data: {json.dumps(dict(second_event, event_id='safe-event-2'))}",
            "unknown: safe-value",
            "",
        ),
        (
            "id: safe-event-2",
            "event: run.created",
            f"data: {json.dumps(dict(second_event, event_id='safe-event-2'))}",
            "retry: 1000",
            "",
        ),
    )
    for invalid_frame in invalid_frames:
        with pytest.raises(AssertionError) as frame_failure:
            _sse_payloads(iter(invalid_frame), database)
        _assert_failure_is_redacted(
            frame_failure,
            tuple(line for line in invalid_frame if line),
        )

    unsafe_pairs: tuple[tuple[str, object], ...] = (
        ("raw_sql", "DELETE FROM orders WHERE credential_canary = true"),
        ("sql_text", "sql-text-canary"),
        ("authorization", "authorization-canary"),
        ("api_key", "api-key-canary"),
        ("password", "password-canary"),
        ("access_token", "access-token-canary"),
        ("rows", [["raw-row-canary"]]),
        ("row", ["row-canary"]),
        ("result", "result-canary"),
        ("raw_result", "raw-result-canary"),
        (" raw_sql ", "spaced-key-canary"),
        ("\uff52\uff41\uff57\uff3f\uff53\uff51\uff4c", "fullwidth-key-canary"),
        ("raw sql", "whitespace-key-canary"),
        ("raw-sql", "hyphen-key-canary"),
    )
    for field_name, sensitive_value in unsafe_pairs:
        candidate = {"safe_arguments": [[field_name, sensitive_value]]}
        with pytest.raises(AssertionError) as pair_failure:
            _assert_safe_tree(candidate)
        candidate_wire = json.dumps(candidate)
        _assert_failure_is_redacted(
            pair_failure,
            (str(sensitive_value), candidate_wire),
        )

    real_factory = create_async_database_engine
    probe = _LifespanProbe()
    engines: list[_SpyEngine] = []

    def engine_factory(settings: DatabaseSettings) -> AsyncEngine:
        probe.record("engine.factory")
        engine = _SpyEngine(real_factory(settings), probe)
        engines.append(engine)
        return cast(AsyncEngine, engine)

    def backend_factory(engine: AsyncEngine) -> _GatedBackend:
        return _GatedBackend(engine, probe)

    def event_store_factory() -> _SpyEventStore:
        probe.record("event_store.construct")
        probe.execution_gate = asyncio.Event()
        probe.live_read_gate = asyncio.Event()
        store = _SpyEventStore(probe)
        probe.event_store = store
        return store

    monkeypatch.setattr(dependencies, "create_async_database_engine", engine_factory)
    monkeypatch.setattr(dependencies, "AsyncEngineSqlExecutionBackend", backend_factory)
    monkeypatch.setattr(dependencies, "InMemoryEventStore", event_store_factory)
    monkeypatch.setattr(
        dependencies,
        "AnalysisRunner",
        partial(_SpyRunner, probe=probe),
    )
    monkeypatch.setattr(dependencies, "builtin_demo_scripts", lambda: API_SCRIPTS)
    runtime = AgentRuntimeSettings(_env_file=None)  # type: ignore[call-arg]
    credential_wire = json.dumps({"api_key": _CREDENTIAL_CANARY})
    with pytest.raises(AssertionError) as credential_failure:
        _assert_serialized_output_has_no_secrets_or_sql(credential_wire, database)
    _assert_failure_is_redacted(
        credential_failure,
        (_CREDENTIAL_CANARY, credential_wire),
    )
    sql_variant = " \n ".join(_SEGMENT_SQL.upper().split())
    sql_wire = json.dumps({"answer": sql_variant})
    with pytest.raises(AssertionError) as sql_failure:
        _assert_serialized_output_has_no_secrets_or_sql(sql_wire, database)
    _assert_failure_is_redacted(
        sql_failure,
        (sql_variant, sql_wire, "SEGMENT_GMV"),
    )
    commented_sql = _SEGMENT_SQL.replace("with segment_gmv", "WITH /*review*/ segment_gmv")
    commented_sql_wire = json.dumps({"answer": commented_sql})
    with pytest.raises(AssertionError) as commented_sql_failure:
        _assert_serialized_output_has_no_secrets_or_sql(commented_sql_wire, database)
    _assert_failure_is_redacted(
        commented_sql_failure,
        (commented_sql, commented_sql_wire, "/*review*/"),
    )
    partial_sql = "WITH segment_gmv AS (...) SELECT previous_gmv, current_gmv FROM customers"
    partial_sql_wire = json.dumps({"answer": partial_sql})
    with pytest.raises(AssertionError) as partial_sql_failure:
        _assert_serialized_output_has_no_secrets_or_sql(partial_sql_wire, database)
    _assert_failure_is_redacted(
        partial_sql_failure,
        (partial_sql, partial_sql_wire),
    )
    credential_label = "authorization: Bearer structured-credential-canary"
    credential_label_wire = json.dumps({"answer": credential_label})
    with pytest.raises(AssertionError) as credential_label_failure:
        _assert_serialized_output_has_no_secrets_or_sql(credential_label_wire, database)
    _assert_failure_is_redacted(
        credential_label_failure,
        (credential_label, credential_label_wire, "structured-credential-canary"),
    )
    legal_wire = {
        "answer": "select the best evidence, completed with limitations",
        "evidence": {"limitations": ["completed with limitations"]},
    }
    _assert_safe_tree(legal_wire)
    _assert_serialized_output_has_no_secrets_or_sql(
        json.dumps(legal_wire),
        database,
    )
    order_canary = "task11-fix3-order-canary"
    unsafe_order_response = httpx.Response(
        200,
        json={"api_key": order_canary, "semantic_field": "unexpected"},
    )
    semantic_assertion_reached = False
    with pytest.raises(AssertionError) as order_failure:
        _safe_response_json(unsafe_order_response, database)
        semantic_assertion_reached = True
    _safe_output_require(not semantic_assertion_reached)
    _assert_failure_is_redacted(
        order_failure,
        (order_canary, unsafe_order_response.text),
    )
    model = ModelSettings(  # type: ignore[call-arg]
        _env_file=None,
        model_api_key=SecretStr(_CREDENTIAL_CANARY),
    )
    app = create_app(
        container_factory=partial(
            dependencies.build_container,
            runtime_settings=runtime,
            database_settings=database,
            model_settings=model,
        )
    )
    assert probe.resource_events == []

    with TestClient(app) as client:
        portal = client.portal
        assert portal is not None
        with pytest.raises(TimeoutError, match="operation-owned-timeout"):
            portal.call(
                partial(
                    _await_with_deadline,
                    _raise_operation_timeout(),
                    seconds=1.0,
                    failure_message=_SSE_DEADLINE_ERROR,
                )
            )
        _safe_output_require(
            portal.call(_tracked_close_failure_probe)
            == _TrackedCloseProbeResult(runtime_error_observed=True, active_leases=0)
        )
        _safe_output_require(portal.call(_close_resists_repeated_cancellation_probe) is True)
        _safe_output_require(portal.call(_blocking_cleanup_fault_probe) is True)
        event_store = probe.event_store
        assert event_store is not None
        ready = client.get("/readyz")
        assert ready.status_code == 200
        _safe_output_require(
            portal.call(
                _exercise_real_sse_timeout,
                app,
                event_store,
                probe,
                database,
            )
            is True
        )

        created = client.post("/v1/analyses", json={"query": ATTRIBUTION_QUERY})
        assert created.status_code == 202
        created_body = _safe_response_json(created, database)
        _assert_exact_keys(created_body, _CREATE_KEYS)
        events_url = created_body["events_url"]
        status_url = created_body["status_url"]
        trace_url = created_body["trace_url"]
        run_id = created_body["run_id"]
        _safe_output_require(isinstance(events_url, str))
        _safe_output_require(isinstance(status_url, str))
        _safe_output_require(isinstance(trace_url, str))
        _safe_output_require(isinstance(run_id, str))
        assert len(engines) == 1
        assert engines[0].dispose_calls == 0

        sse_status, content_type, events = portal.call(
            _consume_sse_with_deadline,
            app,
            cast(str, events_url),
            probe,
            database,
        )
        _safe_output_require(sse_status == 200)
        _safe_output_require(content_type.startswith("text/event-stream"))

        status = client.get(cast(str, status_url))
        trace = client.get(cast(str, trace_url))
        assert status.status_code == trace.status_code == 200
        status_body = _safe_response_json(status, database)
        trace_body = _safe_response_json(trace, database)
        _assert_wire_allowlists(created_body, events, status_body, trace_body)

        sequences = tuple(cast(int, event["sequence"]) for event in events)
        terminal = tuple(event for event in events if event["type"] == "run.terminal")
        _safe_output_require(sequences == tuple(range(1, len(events) + 1)))
        _safe_output_require(len(terminal) == 1)
        _safe_output_require(events[-1] == terminal[0])
        _safe_output_require(status_body["lifecycle_status"] == "terminal")
        _safe_output_require(status_body["final_status"] == "completed")
        _safe_output_require(status_body["stop_reason"] == "answer_complete")
        _safe_output_require(trace_body["snapshot_complete"] is True)
        _safe_output_require(trace_body["stop_reason"] == "answer_complete")
        _safe_output_require(trace_body["tool_call_count"] == 6)
        tool_calls = cast(list[Mapping[str, object]], trace_body["tool_calls"])
        _safe_output_require(sum(tool["tool_name"] == "execute_sql" for tool in tool_calls) == 4)
        assert engines[0].connect_calls == 4
        assert engines[0].dispose_calls == 0
        assert probe.open_nonterminal is True
        assert probe.open_high_water >= 1
        assert probe.live_wait_started is True
        _safe_output_require(any(sequence > probe.open_high_water for sequence in sequences))
        _safe_output_require(probe.active_leases == 0)

        _safe_output_require(portal.call(event_store.delete_run, cast(str, run_id)) is True)

        wire = {
            "created": created_body,
            "events": list(events),
            "status": status_body,
            "trace": trace_body,
        }
        _assert_safe_tree(wire)
        _assert_serialized_output_has_no_secrets_or_sql(
            json.dumps(wire, ensure_ascii=False, sort_keys=True),
            database,
        )

    assert len(engines) == 1
    assert engines[0].dispose_calls == 1
    resource_events = probe.resource_events
    assert len({(event.thread_id, event.loop_id) for event in resource_events}) == 1
    names = [event.name for event in resource_events]
    assert names.count("engine.factory") == 1
    assert names.count("event_store.construct") == 1
    assert names.count("runner.construct") == 1
    assert names.count("backend.execute") == 4
    assert names.count("engine.connect") == 4
    assert names.count("runner.shutdown") == 1
    assert names.count("engine.dispose") == 1
    assert names.index("engine.factory") < names.index("event_store.construct")
    assert names.index("event_store.construct") < names.index("runner.construct")
    assert names.index("runner.construct") < names.index("backend.execute")
    assert names.index("backend.execute") < names.index("engine.connect")
    assert names.index("engine.connect") < names.index("runner.shutdown")
    assert names.index("runner.shutdown") < names.index("engine.dispose")
