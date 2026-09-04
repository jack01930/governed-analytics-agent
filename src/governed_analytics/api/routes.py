"""Versioned analysis, SSE, trace, and health routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse
from starlette.types import Message

from governed_analytics.agent.contracts import RunLifecycleStatus
from governed_analytics.api.contracts import (
    AnalysisCreateRequest,
    AnalysisCreateResponse,
    AnalysisStatusResponse,
    HealthResponse,
    SafeEvidenceResponse,
    SafeModelCallTrace,
    SafeNodeTrace,
    SafeToolTrace,
    TraceResponse,
)
from governed_analytics.api.dependencies import AppContainer
from governed_analytics.runtime.events import (
    EventCursorAhead,
    InvalidEventCursor,
    RunEvent,
    parse_last_event_id,
)
from governed_analytics.runtime.events import RunNotFound as EventRunNotFound
from governed_analytics.runtime.runs import (
    RunCapacityExceeded,
    RunConsistencyError,
    RunnerShutdown,
    RunNotFound,
    RunRecord,
)

analysis_router = APIRouter(prefix="/v1/analyses", tags=["analyses"])
health_router = APIRouter(tags=["health"])


class _SseEventIterator:
    def __init__(self, opened: AsyncIterator[RunEvent]) -> None:
        self._opened = opened
        self._closed = False

    def __aiter__(self) -> _SseEventIterator:
        return self

    async def __anext__(self) -> dict[str, str]:
        if self._closed:
            raise StopAsyncIteration
        try:
            event = await anext(self._opened)
        except BaseException:
            await self.aclose()
            raise
        return {
            "id": event.event_id,
            "event": event.type,
            "data": event.model_dump_json(),
        }

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._opened, "aclose", None)
        if callable(close):
            await close()


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, AppContainer):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service_not_ready",
        )
    return container


ContainerDependency = Annotated[AppContainer, Depends(get_container)]


def _map_run_read_error(error: Exception) -> HTTPException:
    if isinstance(error, RunNotFound):
        return HTTPException(status_code=404, detail="run_not_found")
    if isinstance(error, RunConsistencyError):
        return HTTPException(status_code=503, detail="run_consistency_unavailable")
    raise error


def _status_response(record: RunRecord) -> AnalysisStatusResponse:
    evidence = tuple(
        SafeEvidenceResponse.model_validate(item.model_dump(exclude={"verified"}))
        for item in record.evidence
    )
    return AnalysisStatusResponse(
        run_id=record.run_id,
        lifecycle_status=record.lifecycle_status,
        final_status=record.final_status,
        answer=record.answer,
        evidence=evidence,
        limitations=record.limitations,
        stop_reason=record.stop_reason,
    )


def _trace_response(record: RunRecord) -> TraceResponse:
    if record.lifecycle_status is not RunLifecycleStatus.TERMINAL:
        return TraceResponse(
            run_id=record.run_id,
            snapshot_complete=False,
            nodes=(),
            model_calls=(),
            tool_calls=(),
            evidence_gaps=(),
            repair_count=0,
            model_call_count=0,
            tool_call_count=0,
            input_tokens=0,
            output_tokens=0,
            committed_cost_cny=Decimal("0"),
            stop_reason=None,
        )
    governance = record.governance
    trace = record.safe_trace
    return TraceResponse(
        run_id=record.run_id,
        snapshot_complete=True,
        nodes=tuple(SafeNodeTrace.model_validate(item.model_dump()) for item in trace.nodes),
        model_calls=tuple(
            SafeModelCallTrace.model_validate(item.model_dump()) for item in trace.model_calls
        ),
        tool_calls=tuple(
            SafeToolTrace.model_validate(item.model_dump()) for item in trace.tool_calls
        ),
        evidence_gaps=record.evidence_gaps,
        repair_count=record.repair_count,
        model_call_count=governance.llm_calls,
        tool_call_count=governance.tool_calls,
        input_tokens=governance.input_tokens,
        output_tokens=governance.output_tokens,
        committed_cost_cny=governance.committed_cost_cny,
        stop_reason=record.stop_reason,
    )


@analysis_router.post(
    "",
    response_model=AnalysisCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_analysis(
    payload: AnalysisCreateRequest,
    container: ContainerDependency,
) -> AnalysisCreateResponse:
    try:
        record = await container.runner.submit(payload.query)
    except RunCapacityExceeded:
        raise HTTPException(status_code=503, detail="run_capacity_exceeded") from None
    except RunnerShutdown:
        raise HTTPException(status_code=503, detail="service_not_ready") from None
    except RunConsistencyError:
        raise HTTPException(status_code=503, detail="run_consistency_unavailable") from None
    prefix = f"/v1/analyses/{record.run_id}"
    return AnalysisCreateResponse(
        run_id=record.run_id,
        lifecycle_status="queued",
        status_url=prefix,
        events_url=f"{prefix}/events",
        trace_url=f"{prefix}/trace",
    )


@analysis_router.get("/{run_id}", response_model=AnalysisStatusResponse)
async def get_analysis(
    run_id: str,
    container: ContainerDependency,
) -> AnalysisStatusResponse:
    try:
        record = await container.runner.get(run_id)
    except (RunNotFound, RunConsistencyError) as error:
        raise _map_run_read_error(error) from None
    return _status_response(record)


@analysis_router.get("/{run_id}/trace", response_model=TraceResponse)
async def get_trace(
    run_id: str,
    container: ContainerDependency,
) -> TraceResponse:
    try:
        record = await container.runner.get(run_id)
    except (RunNotFound, RunConsistencyError) as error:
        raise _map_run_read_error(error) from None
    return _trace_response(record)


@analysis_router.get("/{run_id}/events")
async def sse_events(
    run_id: str,
    container: ContainerDependency,
    last_event_id: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    try:
        high_water = await container.events.high_water_mark(run_id)
        after = parse_last_event_id(
            run_id,
            last_event_id,
            high_water_mark=high_water,
        )
        opened = await container.events.open_stream(run_id, after_sequence=after)
    except EventRunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found") from None
    except InvalidEventCursor:
        raise HTTPException(status_code=400, detail="invalid_event_cursor") from None
    except EventCursorAhead:
        raise HTTPException(status_code=409, detail="event_cursor_ahead") from None

    iterator = _SseEventIterator(opened)

    async def close_on_disconnect(_message: Message) -> None:
        await iterator.aclose()

    return EventSourceResponse(
        iterator,
        ping=container.settings.sse_heartbeat_seconds,
        client_close_handler_callable=close_on_disconnect,
    )


@health_router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()


@health_router.get("/readyz", response_model=HealthResponse)
async def readyz(container: ContainerDependency) -> HealthResponse:
    del container
    return HealthResponse()


__all__ = [
    "ContainerDependency",
    "analysis_router",
    "get_container",
    "health_router",
]
