from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from governed_analytics.agent.contracts import (
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    NodeTrace,
    RunLifecycleStatus,
    SafeTrace,
    StopReason,
)
from governed_analytics.api.app import create_app
from governed_analytics.api.dependencies import AppContainer
from governed_analytics.api.routes import get_container, sse_events
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.runtime.events import InMemoryEventStore, RunEvent
from governed_analytics.runtime.events import RunNotFound as EventRunNotFound
from governed_analytics.runtime.runs import (
    RunCapacityExceeded,
    RunConsistencyError,
    RunNotFound,
    RunRecord,
)

NOW = datetime(2026, 9, 4, tzinfo=UTC)


def queued(run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        query="safe query",
        lifecycle_status=RunLifecycleStatus.QUEUED,
        created_at=NOW,
    )


def terminal(run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        query="safe query",
        lifecycle_status=RunLifecycleStatus.TERMINAL,
        created_at=NOW,
        started_at=NOW,
        terminal_at=NOW,
        final_status=FinalStatus.COMPLETED,
        answer="分析完成。",
        limitations=("fixture only",),
        evidence_gaps=(),
        repair_count=0,
        governance=GovernanceSnapshot(llm_calls=1),
        stop_reason=StopReason.ANSWER_COMPLETE,
        safe_trace=SafeTrace(
            nodes=(NodeTrace(node="finalize", duration_ms=1, outcome="completed"),)
        ),
    )


class FakeRunner:
    def __init__(self, records: dict[str, RunRecord] | None = None) -> None:
        self.records = records or {}
        self.capacity_error = False
        self.consistency_error = False

    async def submit(self, query: str) -> RunRecord:
        del query
        if self.capacity_error:
            raise RunCapacityExceeded()
        record = queued(f"run-{len(self.records) + 1}")
        self.records[record.run_id] = record
        return record

    async def get(self, run_id: str) -> RunRecord:
        if self.consistency_error:
            raise RunConsistencyError()
        try:
            return self.records[run_id]
        except KeyError:
            raise RunNotFound() from None

    async def shutdown(self) -> None:
        return None


def event(sequence: int) -> RunEvent:
    definitions: dict[int, tuple[str, str, Mapping[str, JsonValue]]] = {
        1: ("runtime", "run.created", {"status": "queued"}),
        2: ("runtime", "run.started", {"status": "running"}),
        3: (
            "decide_behavior",
            "behavior.decided",
            {"action": "execute", "reason_code": "ready", "missing_fields": ()},
        ),
        4: (
            "runtime",
            "run.terminal",
            {"final_status": "completed", "stop_reason": "answer_complete"},
        ),
    }
    node, event_type, data = definitions[sequence]
    return RunEvent(
        event_id=f"run-1:{sequence}",
        sequence=sequence,
        run_id="run-1",
        timestamp=NOW,
        node=node,
        type=event_type,
        data=data,
    )


class CannedEventStore:
    def __init__(self) -> None:
        self.events = tuple(event(sequence) for sequence in range(1, 5))

    async def high_water_mark(self, run_id: str, *, owner_token: object | None = None) -> int:
        del owner_token
        if run_id != "run-1":
            raise EventRunNotFound()
        return len(self.events)

    async def open_stream(
        self, run_id: str, *, after_sequence: int | None
    ) -> AsyncIterator[RunEvent]:
        if run_id != "run-1":
            raise EventRunNotFound()

        async def generate() -> AsyncIterator[RunEvent]:
            for item in self.events:
                if item.sequence > (after_sequence or 0):
                    yield item

        return generate()


class PrunedBeforeOpenEventStore(CannedEventStore):
    async def open_stream(
        self, run_id: str, *, after_sequence: int | None
    ) -> AsyncIterator[RunEvent]:
        del run_id, after_sequence
        raise EventRunNotFound()


class ExceptionalOpenedStream:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> ExceptionalOpenedStream:
        return self

    async def __anext__(self) -> RunEvent:
        raise RuntimeError("stream failed")

    async def aclose(self) -> None:
        self.closed = True


class ExceptionalEventStore(CannedEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.opened = ExceptionalOpenedStream()

    async def open_stream(
        self, run_id: str, *, after_sequence: int | None
    ) -> AsyncIterator[RunEvent]:
        del run_id, after_sequence
        return self.opened


class PendingReadEventStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_started = asyncio.Event()

    async def _stream_buffer(
        self,
        buffer: Any,
        *,
        after_sequence: int | None,
    ) -> AsyncGenerator[RunEvent, None]:
        self.read_started.set()
        async for item in super()._stream_buffer(
            buffer,
            after_sequence=after_sequence,
        ):
            yield item


def factory_for(
    runner: FakeRunner,
    events: CannedEventStore | None = None,
) -> Callable[[], AbstractAsyncContextManager[AppContainer]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[AppContainer]:
        yield AppContainer(
            settings=AgentRuntimeSettings(  # type: ignore[call-arg]
                _env_file=None,
                sse_heartbeat_seconds=1,
            ),
            runner=runner,  # type: ignore[arg-type]
            events=events or CannedEventStore(),  # type: ignore[arg-type]
        )

    return factory


def parse_sse(text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            name, _, value = line.partition(":")
            fields[name] = value.lstrip()
        messages.append(fields)
    return messages


def direct_container(events: object, runner: FakeRunner | None = None) -> AppContainer:
    return AppContainer(
        settings=AgentRuntimeSettings(  # type: ignore[call-arg]
            _env_file=None,
            sse_heartbeat_seconds=1,
        ),
        runner=runner or FakeRunner(),  # type: ignore[arg-type]
        events=events,  # type: ignore[arg-type]
    )


def test_post_analysis_returns_queued_urls_and_filters_response() -> None:
    runner = FakeRunner()
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        response = client.post(
            "/v1/analyses",
            json={"query": "2026年6月GMV是多少？"},  # noqa: RUF001
        )

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "run_id": "run-1",
        "lifecycle_status": "queued",
        "status_url": "/v1/analyses/run-1",
        "events_url": "/v1/analyses/run-1/events",
        "trace_url": "/v1/analyses/run-1/trace",
    }


def test_all_container_routes_use_overridable_annotated_dependency() -> None:
    base_runner = FakeRunner()
    override_runner = FakeRunner({"run-1": terminal()})
    application = create_app(container_factory=factory_for(base_runner))
    override = direct_container(CannedEventStore(), override_runner)
    application.dependency_overrides[get_container] = lambda: override

    with TestClient(application) as client:
        response = client.get("/v1/analyses/run-1")

    assert response.status_code == 200
    container_paths = {
        "/v1/analyses",
        "/v1/analyses/{run_id}",
        "/v1/analyses/{run_id}/trace",
        "/v1/analyses/{run_id}/events",
        "/readyz",
    }
    included_routes = (
        route
        for included in application.routes
        for route in getattr(getattr(included, "original_router", None), "routes", ())
    )
    routes = {
        route.path: route
        for route in included_routes
        if isinstance(route, APIRoute) and route.path in container_paths
    }
    assert routes.keys() == container_paths
    for route in routes.values():
        assert any(dependency.call is get_container for dependency in route.dependant.dependencies)


def test_post_maps_capacity_and_rejects_request_overrides() -> None:
    runner = FakeRunner()
    runner.capacity_error = True
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        capacity = client.post("/v1/analyses", json={"query": "query"})
        override = client.post(
            "/v1/analyses",
            json={"query": "query", "runtime_mode": "live"},
        )

    assert capacity.status_code == 503
    assert capacity.json() == {"detail": "run_capacity_exceeded"}
    assert override.status_code == 422


def test_request_validation_error_is_fixed_and_redacts_raw_input() -> None:
    sentinel = "secret-payload-prompt-sentinel"
    runner = FakeRunner()
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        response = client.post(
            "/v1/analyses",
            json={
                "query": "safe query",
                "model_api_key": sentinel,
                "payload": {"prompt": sentinel},
                "prompt": sentinel,
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "request_validation_failed"}
    serialized = json.dumps(response.json()).casefold()
    for forbidden in (
        "query",
        "model_api_key",
        "payload",
        "prompt",
        sentinel,
        "ctx",
        "input",
    ):
        assert forbidden not in serialized


def test_status_unknown_terminal_and_consistency_mapping_are_safe() -> None:
    runner = FakeRunner({"run-1": terminal()})
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        response = client.get("/v1/analyses/run-1")
        unknown = client.get("/v1/analyses/unknown")
        runner.consistency_error = True
        inconsistent = client.get("/v1/analyses/run-1")
        inconsistent_trace = client.get("/v1/analyses/run-1/trace")

    assert response.status_code == 200
    assert response.json()["final_status"] == "completed"
    assert "query" not in response.json()
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "run_not_found"}
    assert inconsistent.status_code == 503
    assert inconsistent.json() == {"detail": "run_consistency_unavailable"}
    assert inconsistent_trace.status_code == 503
    assert inconsistent_trace.json() == {"detail": "run_consistency_unavailable"}
    assert "safe query" not in json.dumps((inconsistent.json(), inconsistent_trace.json()))


def test_trace_has_explicit_initial_and_terminal_frozen_snapshots() -> None:
    runner = FakeRunner({"queued": queued("queued"), "run-1": terminal()})
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        initial = client.get("/v1/analyses/queued/trace")
        complete = client.get("/v1/analyses/run-1/trace")

    assert initial.status_code == 200
    assert initial.json() == {
        "run_id": "queued",
        "snapshot_complete": False,
        "nodes": [],
        "model_calls": [],
        "tool_calls": [],
        "evidence_gaps": [],
        "repair_count": 0,
        "model_call_count": 0,
        "tool_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "committed_cost_cny": "0",
        "stop_reason": None,
    }
    body = complete.json()
    assert body["snapshot_complete"] is True
    assert body["nodes"] == [{"node": "finalize", "duration_ms": 1, "outcome": "completed"}]
    serialized = json.dumps(body)
    for forbidden in ("sql", "parameters", "rows", "prompt", "endpoint", "provider_raw"):
        assert forbidden not in serialized.casefold()


@pytest.mark.parametrize(
    ("header", "expected_sequences"),
    [(None, [1, 2, 3, 4]), ("run-1:2", [3, 4])],
)
def test_sse_replay_uses_identical_protocol_and_envelope_ids(
    header: str | None, expected_sequences: list[int]
) -> None:
    runner = FakeRunner({"run-1": terminal()})
    headers = {} if header is None else {"Last-Event-ID": header}
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        response = client.get("/v1/analyses/run-1/events", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = parse_sse(response.text)
    envelopes = [json.loads(message["data"]) for message in messages]
    assert [envelope["sequence"] for envelope in envelopes] == expected_sequences
    assert [message["id"] for message in messages] == [
        envelope["event_id"] for envelope in envelopes
    ]
    assert messages[-1]["event"] == "run.terminal"


@pytest.mark.parametrize(
    ("run_id", "header", "status", "detail"),
    [
        ("unknown", None, 404, "run_not_found"),
        ("run-1", "other:2", 400, "invalid_event_cursor"),
        ("run-1", "invalid", 400, "invalid_event_cursor"),
        ("run-1", "run-1:5", 409, "event_cursor_ahead"),
        ("run-1", f"run-1:{2**63}", 400, "invalid_event_cursor"),
    ],
)
def test_sse_maps_cursor_and_unknown_errors(
    run_id: str, header: str | None, status: int, detail: str
) -> None:
    runner = FakeRunner({"run-1": terminal()})
    headers = {} if header is None else {"Last-Event-ID": header}
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        response = client.get(f"/v1/analyses/{run_id}/events", headers=headers)

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_sse_ttl_prune_race_maps_late_not_found_before_response_headers() -> None:
    runner = FakeRunner({"run-1": terminal()})
    events = PrunedBeforeOpenEventStore()
    with TestClient(create_app(container_factory=factory_for(runner, events))) as client:
        response = client.get("/v1/analyses/run-1/events")

    assert response.status_code == 404
    assert response.json() == {"detail": "run_not_found"}


@pytest.mark.asyncio
async def test_sse_response_body_iterator_closes_lease_before_first_iteration() -> None:
    events = InMemoryEventStore()
    owner = object()
    await events.create_run("run-1", owner_token=owner)
    await events.emit_terminal(
        "run-1",
        {"final_status": "completed", "stop_reason": "answer_complete"},
        owner_token=owner,
    )
    response = await sse_events("run-1", direct_container(events))

    assert await events.delete_run("run-1", owner_token=owner) is False
    await cast(Any, response.body_iterator).aclose()
    assert await events.delete_run("run-1", owner_token=owner) is True


@pytest.mark.asyncio
async def test_sse_response_body_iterator_closes_after_start_and_natural_end() -> None:
    for close_early in (True, False):
        events = InMemoryEventStore()
        owner = object()
        await events.create_run("run-1", owner_token=owner)
        await events.emit_terminal(
            "run-1",
            {"final_status": "completed", "stop_reason": "answer_complete"},
            owner_token=owner,
        )
        response = await sse_events("run-1", direct_container(events))
        iterator = cast(Any, response.body_iterator)
        await anext(iterator)
        if close_early:
            await iterator.aclose()
        else:
            async for _item in iterator:
                pass

        assert await events.delete_run("run-1", owner_token=owner) is True


@pytest.mark.asyncio
async def test_sse_response_asgi_disconnect_closes_lease_while_send_is_blocked() -> None:
    events = InMemoryEventStore()
    owner = object()
    await events.create_run("run-1", owner_token=owner)
    await events.emit_terminal(
        "run-1",
        {"final_status": "completed", "stop_reason": "answer_complete"},
        owner_token=owner,
    )
    response = await sse_events("run-1", direct_container(events))
    body_send_started = asyncio.Event()
    never_finish_send = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            body_send_started.set()
            await never_finish_send.wait()

    async def receive() -> Message:
        await body_send_started.wait()
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/analyses/run-1/events",
        "raw_path": b"/v1/analyses/run-1/events",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await response(
        scope,
        receive,
        send,
    )

    assert await events.delete_run("run-1", owner_token=owner) is True


@pytest.mark.asyncio
async def test_sse_response_disconnect_while_next_event_is_pending_releases_lease() -> None:
    events = PendingReadEventStore()
    owner = object()
    await events.create_run("run-1", owner_token=owner)
    response = await sse_events("run-1", direct_container(events))
    iterator = cast(Any, response.body_iterator)

    async def send(_message: Message) -> None:
        return None

    async def receive() -> Message:
        await events.read_started.wait()
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/analyses/run-1/events",
        "raw_path": b"/v1/analyses/run-1/events",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    await asyncio.sleep(0)

    assert events._runs["run-1"].active_streams == 0
    assert iterator._opened._read_task is None
    assert iterator._opened._close_task.done()
    assert await events.delete_run(
        "run-1",
        allow_unstarted=True,
        owner_token=owner,
    )


@pytest.mark.asyncio
async def test_sse_response_disconnect_races_with_terminal_event_arrival() -> None:
    async def run_race(allow_arrival_to_run: bool) -> None:
        events = PendingReadEventStore()
        owner = object()
        await events.create_run("run-1", owner_token=owner)
        response = await sse_events("run-1", direct_container(events))
        arrival: asyncio.Task[RunEvent] | None = None

        async def send(_message: Message) -> None:
            return None

        async def receive() -> Message:
            nonlocal arrival
            await events.read_started.wait()
            arrival = asyncio.create_task(
                events.emit_terminal(
                    "run-1",
                    {
                        "final_status": "completed",
                        "stop_reason": "answer_complete",
                    },
                    owner_token=owner,
                )
            )
            if allow_arrival_to_run:
                await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        await asyncio.wait_for(
            response(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/v1/analyses/run-1/events",
                    "raw_path": b"/v1/analyses/run-1/events",
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                },
                receive,
                send,
            ),
            timeout=1,
        )
        assert arrival is not None
        await arrival
        assert await events.delete_run("run-1", owner_token=owner)

    await run_race(False)
    await run_race(True)


@pytest.mark.asyncio
async def test_sse_response_task_cancellation_and_close_share_one_completion() -> None:
    events = PendingReadEventStore()
    owner = object()
    await events.create_run("run-1", owner_token=owner)
    response = await sse_events("run-1", direct_container(events))
    iterator = cast(Any, response.body_iterator)
    read = asyncio.create_task(anext(iterator))
    await events.read_started.wait()

    read.cancel()
    results = await asyncio.gather(
        read,
        iterator.aclose(),
        iterator.aclose(),
        return_exceptions=True,
    )

    assert isinstance(results[0], asyncio.CancelledError)
    assert all(result is None for result in results[1:])
    await iterator.aclose()
    assert await events.delete_run(
        "run-1",
        allow_unstarted=True,
        owner_token=owner,
    )


@pytest.mark.asyncio
async def test_sse_response_unstarted_sequential_and_concurrent_close_is_idempotent() -> None:
    events = InMemoryEventStore()
    owner = object()
    await events.create_run("run-1", owner_token=owner)
    response = await sse_events("run-1", direct_container(events))
    iterator = cast(Any, response.body_iterator)

    results = await asyncio.gather(iterator.aclose(), iterator.aclose())
    assert all(result is None for result in results)
    await iterator.aclose()
    await iterator.aclose()
    assert await events.delete_run(
        "run-1",
        allow_unstarted=True,
        owner_token=owner,
    )


@pytest.mark.asyncio
async def test_sse_response_body_iterator_closes_opened_stream_on_exception() -> None:
    events = ExceptionalEventStore()
    response = await sse_events("run-1", direct_container(events))

    with pytest.raises(RuntimeError, match="stream failed"):
        await anext(cast(Any, response.body_iterator))

    assert events.opened.closed
