from __future__ import annotations

import asyncio
import contextlib
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

import governed_analytics.api.dependencies as dependencies
from governed_analytics.api.app import create_app
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.models.agent_fixtures import AgentScripts
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.runtime.events import InMemoryEventStore, RunEvent
from governed_analytics.runtime.runs import AnalysisRunner
from governed_analytics.safety.sql_policy import ValidatedSql
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


async def _await_with_deadline[T](
    operation: Awaitable[T],
    *,
    seconds: float,
    failure_message: str,
) -> T:
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            return await operation
    except TimeoutError:
        if not deadline.expired():
            raise
        raise AssertionError(failure_message) from None


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


class _BlockingCloseLines:
    def __init__(self) -> None:
        self.close_gate = asyncio.Event()
        self.cancellations = 0

    def __aiter__(self) -> _BlockingCloseLines:
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        while True:
            try:
                await self.close_gate.wait()
                return
            except asyncio.CancelledError:
                self.cancellations += 1
                if self.cancellations >= 2:
                    raise


async def _tracked_close_failure_probe() -> int:
    probe = _LifespanProbe(active_leases=1)
    tracked = _TrackedEventStream(_CloseRaisesStream(), probe, open_high_water=0)
    with contextlib.suppress(RuntimeError):
        await tracked.aclose()
    return probe.active_leases


async def _blocking_cleanup_fault_probe() -> bool:
    stream = _BlockingCloseLines()
    try:
        await _collect_sse_lines(stream, cleanup_seconds=0.02)
    except AssertionError as failure:
        pending_cleanup = any(
            task.get_name() == "sse-test-cleanup" and not task.done()
            for task in asyncio.all_tasks()
        )
        return (
            str(failure) == _CLEANUP_DEADLINE_ERROR
            and stream.cancellations == 2
            and not pending_cleanup
        )
    return False


async def _raise_operation_timeout() -> None:
    raise TimeoutError("operation-owned-timeout")


async def _close_with_deadline(
    close: Callable[[], Awaitable[object]],
    *,
    seconds: float,
) -> None:
    cleanup = asyncio.ensure_future(close())
    cleanup.set_name("sse-test-cleanup")
    done, _ = await asyncio.wait({cleanup}, timeout=seconds)
    if done:
        await cleanup
        return

    cleanup.cancel()
    done, _ = await asyncio.wait({cleanup}, timeout=seconds)
    if not done:
        cleanup.cancel()
        done, _ = await asyncio.wait({cleanup}, timeout=seconds)
    if not done:
        cleanup.add_done_callback(lambda task: None if task.cancelled() else task.exception())
    else:
        await asyncio.gather(cleanup, return_exceptions=True)
    raise AssertionError(_CLEANUP_DEADLINE_ERROR)


async def _collect_sse_lines(
    lines: AsyncIterator[str],
    *,
    cleanup_seconds: float = _CLEANUP_DEADLINE_SECONDS,
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
    run_id = "integration-real-sse-timeout"
    await event_store.create_unstarted_run(run_id)
    current = asyncio.current_task()
    pending_before = _pending_task_ids(current)
    try:
        await _consume_sse_with_deadline(
            app,
            f"/v1/analyses/{run_id}/events",
            probe,
            database,
            0.05,
        )
    except AssertionError as failure:
        _safe_output_require(str(failure) == _SSE_DEADLINE_ERROR)
    else:
        _safe_output_require(False)

    pending_after: frozenset[int] = frozenset()
    for _ in range(4):
        pending_after = _pending_task_ids(current)
        if pending_after == pending_before:
            break
        checkpoint = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
        await checkpoint
    _safe_output_require(pending_after == pending_before)
    shutdown_watchers = tuple(
        task for task in asyncio.all_tasks() if not task.done() and _is_sse_shutdown_watcher(task)
    )
    _safe_output_require(len(shutdown_watchers) <= 1)
    _safe_output_require(probe.open_nonterminal)
    _safe_output_require(probe.live_wait_started)
    _safe_output_require(probe.active_leases == 0)
    execution_gate = probe.execution_gate
    live_read_gate = probe.live_read_gate
    _safe_output_require(
        execution_gate is not None
        and execution_gate.is_set()
        and live_read_gate is not None
        and live_read_gate.is_set()
    )
    deleted = await event_store.delete_run(run_id, allow_unstarted=True)
    _safe_output_require(deleted)
    event_store.reset_stream_gates()
    return True


def _is_sse_shutdown_watcher(task: asyncio.Task[object]) -> bool:
    code = getattr(task.get_coro(), "cr_code", None)
    return getattr(code, "co_name", None) == "_shutdown_watcher" and str(
        getattr(code, "co_filename", "")
    ).endswith("sse_starlette/sse.py")


def _pending_task_ids(current: asyncio.Task[object] | None) -> frozenset[int]:
    return frozenset(
        id(task)
        for task in asyncio.all_tasks()
        if task is not current and not task.done() and not _is_sse_shutdown_watcher(task)
    )


def _sse_payloads(
    lines: Iterator[str],
    database: DatabaseSettings,
) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    protocol_id: str | None = None
    for line in lines:
        if line.startswith("id: "):
            protocol_id = line.removeprefix("id: ")
        elif line.startswith("data: "):
            serialized_payload = line.removeprefix("data: ")
            try:
                untrusted_payload = json.loads(serialized_payload)
            except json.JSONDecodeError:
                raise AssertionError(_SAFE_OUTPUT_ERROR) from None
            _assert_safe_tree(untrusted_payload)
            _assert_serialized_output_has_no_secrets_or_sql(
                serialized_payload,
                database,
            )
            payload = cast(
                dict[str, object],
                _assert_exact_keys(untrusted_payload, _EVENT_KEYS),
            )
            event_type = payload["type"]
            _safe_output_require(isinstance(event_type, str))
            event_type = cast(str, event_type)
            _safe_output_require(event_type in _EVENT_DATA_KEYS)
            _assert_exact_keys(payload["data"], _EVENT_DATA_KEYS[event_type])
            _safe_output_require(payload["event_id"] == protocol_id)
            payloads.append(payload)
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
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"[\s\-]+", "_", normalized)
    return (
        normalized in _FORBIDDEN_KEYS
        or normalized.endswith("_sql")
        or normalized.startswith("sql_")
    )


def _assert_safe_tree(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _safe_output_require(isinstance(key, str))
            _safe_output_require(not _is_forbidden_field_name(key))
            key = cast(str, key)
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
                _assert_safe_tree(typed_pair[1], path=(*path, pair_name))
            return
        for item in value:
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
    credential_assignment = re.compile(
        r"(?i)(?:\b(?:authorization|api[\s_-]*key|password|access[\s_-]*token)"
        r"\s*[:=]\s*\S+|\bbearer\s+\S+)"
    )
    _safe_output_require(not any(credential_assignment.search(value) for value in string_values))
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
        for item in value.values():
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
            "dimensions": [["raw_sql", "verified dimension value"]],
            "limitations": ["completed with limitations", "select the best evidence"],
        }
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
    database = DatabaseSettings()  # type: ignore[call-arg]
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
        _safe_output_require(portal.call(_tracked_close_failure_probe) == 0)
        _safe_output_require(portal.call(_blocking_cleanup_fault_probe) is True)
        event_store = probe.event_store
        assert event_store is not None
        ready = client.get("/readyz")
        assert ready.status_code == 200
        worker_threads_before = frozenset(
            thread.ident for thread in threading.enumerate() if thread.is_alive()
        )
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
        worker_threads_after = frozenset(
            thread.ident for thread in threading.enumerate() if thread.is_alive()
        )
        assert worker_threads_after == worker_threads_before

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
