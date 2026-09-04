from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

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
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.runtime.events import RunEvent
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

    async def high_water_mark(
        self, run_id: str, *, owner_token: object | None = None
    ) -> int:
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


def test_status_unknown_terminal_and_consistency_mapping_are_safe() -> None:
    runner = FakeRunner({"run-1": terminal()})
    with TestClient(create_app(container_factory=factory_for(runner))) as client:
        response = client.get("/v1/analyses/run-1")
        unknown = client.get("/v1/analyses/unknown")
        runner.consistency_error = True
        inconsistent = client.get("/v1/analyses/run-1")

    assert response.status_code == 200
    assert response.json()["final_status"] == "completed"
    assert "query" not in response.json()
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "run_not_found"}
    assert inconsistent.status_code == 503
    assert inconsistent.json() == {"detail": "run_consistency_unavailable"}


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
    assert body["nodes"] == [
        {"node": "finalize", "duration_ms": 1, "outcome": "completed"}
    ]
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
    with TestClient(
        create_app(container_factory=factory_for(runner, events))
    ) as client:
        response = client.get("/v1/analyses/run-1/events")

    assert response.status_code == 404
    assert response.json() == {"detail": "run_not_found"}
