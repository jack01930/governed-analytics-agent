from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from governed_analytics.runtime.events import (
    BoundEventSink,
    EventCursor,
    EventCursorAhead,
    InMemoryEventStore,
    InvalidEventCursor,
    InvalidRunEvent,
    RunEvent,
    RunNotFound,
    TerminalEventExists,
    parse_last_event_id,
)

VALID_EVENTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("runtime", "run.created", {"status": "queued"}),
    ("runtime", "run.started", {"status": "running"}),
    (
        "decide_behavior",
        "behavior.decided",
        {"action": "execute", "reason_code": "ready", "missing_fields": ()},
    ),
    (
        "retrieve_context",
        "context.retrieved",
        {"metric_count": 15, "table_count": 8, "success": True},
    ),
    (
        "build_plan",
        "plan.created",
        {
            "plan_id": "simple-plan",
            "revision": 1,
            "analysis_type": "simple",
            "metric_id": "gmv",
            "hypothesis_ids": ("metric_value",),
        },
    ),
    (
        "judge_evidence",
        "hypothesis.updated",
        {"hypothesis_id": "metric_value", "status": "supported"},
    ),
    (
        "invoke_tool",
        "tool.started",
        {
            "tool_name": "execute_sql",
            "purpose": "metric_value_contract",
            "contract_id": "metric_value_contract",
        },
    ),
    (
        "invoke_tool",
        "tool.completed",
        {
            "tool_name": "execute_sql",
            "purpose": "metric_value_contract",
            "query_id": "a" * 64,
            "columns": ("gmv",),
            "row_count": 1,
            "possibly_truncated": False,
        },
    ),
    (
        "invoke_tool",
        "tool.failed",
        {
            "tool_name": "execute_sql",
            "purpose": "metric_value_contract",
            "safe_error": "database_error",
        },
    ),
    (
        "validate_observation",
        "observation.validated",
        {
            "contract_id": "metric_value_contract",
            "valid": True,
            "error_code": None,
            "repairable": False,
        },
    ),
    (
        "judge_evidence",
        "evidence.assessed",
        {"verified_count": 1, "gaps": (), "partial": False, "complete": True},
    ),
    (
        "repair",
        "repair.started",
        {"repair_count": 1, "error_code": "column_contract_mismatch", "success": False},
    ),
    (
        "repair",
        "repair.completed",
        {"repair_count": 1, "error_code": "column_contract_mismatch", "success": True},
    ),
    (
        "route_action",
        "budget.warning",
        {
            "reason": "cost_soft_cap",
            "llm_calls": 3,
            "tool_calls": 2,
            "execute_calls": 0,
            "profile_calls": 0,
            "repair_count": 0,
            "committed_cost_cny": "0.20",
        },
    ),
    (
        "runtime",
        "run.terminal",
        {"final_status": "completed", "stop_reason": "answer_complete"},
    ),
)


async def emit_valid(
    store: InMemoryEventStore,
    run_id: str,
    node: str,
    event_type: str,
    data: Mapping[str, Any],
) -> RunEvent:
    if event_type == "run.terminal":
        return await store.emit_terminal(run_id, data)
    return await store.emit(run_id, node, event_type, data)


@pytest.mark.asyncio
async def test_events_are_monotonic_and_terminal_is_unique() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")

    created = await store.emit("run-1", "runtime", "run.created", {"status": "queued"})
    started = await store.emit("run-1", "runtime", "run.started", {"status": "running"})
    terminal = await store.emit_terminal(
        "run-1",
        {"final_status": "completed", "stop_reason": "answer_complete"},
    )

    assert (created.sequence, started.sequence, terminal.sequence) == (1, 2, 3)
    assert terminal.event_id == "run-1:3"
    assert terminal.timestamp.tzinfo is not None
    assert terminal.timestamp.utcoffset() is not None
    with pytest.raises(TerminalEventExists):
        await store.emit_terminal(
            "run-1",
            {"final_status": "internal_error", "stop_reason": "internal_error"},
        )
    with pytest.raises(TerminalEventExists):
        await store.emit("run-1", "runtime", "run.started", {"status": "running"})


def test_public_modules_import_in_a_fresh_process_without_order_dependency() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import governed_analytics.runtime\n"
                "import governed_analytics.runtime.events\n"
                "from governed_analytics.runtime import BudgetLedger\n"
                "from governed_analytics.agent import run_agent\n"
                "assert BudgetLedger is not None and run_agent is not None\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_last_event_id_rejects_cross_run_invalid_and_ahead_cursors() -> None:
    assert parse_last_event_id("run-1", None, high_water_mark=4) is None
    assert parse_last_event_id("run-1", "run-1:0", high_water_mark=4) == 0
    assert parse_last_event_id("run-1", "run-1:2", high_water_mark=4) == 2
    with pytest.raises(InvalidEventCursor):
        parse_last_event_id("run-1", "run-2:2", high_water_mark=4)
    with pytest.raises(EventCursorAhead):
        parse_last_event_id("run-1", "run-1:5", high_water_mark=4)


@pytest.mark.parametrize(
    "value",
    (
        True,
        False,
        "",
        " ",
        ":",
        "run-1:",
        ":1",
        "run-1:+1",
        "run-1:-1",
        "run-1:01",
        "run-1: 1",
        "run-1:1 ",
        "run-1:1:2",
    ),
)
def test_last_event_id_strictly_rejects_cursor_tricks(value: object) -> None:
    with pytest.raises(InvalidEventCursor):
        parse_last_event_id("run-1", value, high_water_mark=4)  # type: ignore[arg-type]


def test_event_cursor_is_strict_and_canonical() -> None:
    cursor = EventCursor(run_id="run-1", sequence=0)
    assert cursor.event_id == "run-1:0"
    with pytest.raises(ValidationError):
        EventCursor(run_id="run-1", sequence=True)


@pytest.mark.asyncio
async def test_bound_sink_cannot_emit_for_another_run_or_runtime() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    await store.create_run("run-2")
    sink = BoundEventSink(store, "run-1")

    await sink.emit(
        "decide_behavior",
        "behavior.decided",
        {"action": "execute", "reason_code": "ready", "missing_fields": ()},
    )

    assert await store.high_water_mark("run-1") == 1
    assert await store.high_water_mark("run-2") == 0
    with pytest.raises(InvalidRunEvent):
        await sink.emit("runtime", "run.created", {"status": "queued"})


@pytest.mark.asyncio
async def test_history_to_live_stream_has_no_loss_or_duplicates() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    await store.emit("run-1", "runtime", "run.created", {"status": "queued"})
    await store.emit("run-1", "runtime", "run.started", {"status": "running"})
    history_read = asyncio.Event()
    third_emitted = asyncio.Event()
    allow_terminal = asyncio.Event()

    async def produce() -> None:
        await history_read.wait()
        await store.emit(
            "run-1",
            "retrieve_context",
            "context.retrieved",
            {"metric_count": 15, "table_count": 8, "success": True},
        )
        third_emitted.set()
        await allow_terminal.wait()
        await store.emit_terminal(
            "run-1",
            {"final_status": "completed", "stop_reason": "answer_complete"},
        )

    producer = asyncio.create_task(produce())
    stream = store.stream("run-1", after_sequence=None)
    received = [await anext(stream), await anext(stream)]
    history_read.set()
    await third_emitted.wait()
    received.append(await anext(stream))
    allow_terminal.set()
    received.append(await anext(stream))
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await producer

    assert tuple(event.sequence for event in received) == (1, 2, 3, 4)
    assert received[-1].type == "run.terminal"


@pytest.mark.asyncio
async def test_active_stream_blocks_delete_then_terminal_stream_allows_retry() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    await store.emit("run-1", "runtime", "run.created", {"status": "queued"})
    stream = store.stream("run-1", after_sequence=None)
    assert (await anext(stream)).sequence == 1
    assert await store.delete_run("run-1", allow_unstarted=True) is False
    await store.emit_terminal(
        "run-1",
        {"final_status": "internal_error", "stop_reason": "internal_error"},
    )
    assert (await anext(stream)).type == "run.terminal"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert await store.delete_run("run-1") is True
    with pytest.raises(RunNotFound):
        await store.high_water_mark("run-1")
    with pytest.raises(RunNotFound):
        await anext(store.stream("run-1", after_sequence=None))


@pytest.mark.asyncio
async def test_unstarted_rollback_and_default_delete_rules() -> None:
    store = InMemoryEventStore()
    await store.create_run("queued")
    await store.emit("queued", "runtime", "run.created", {"status": "queued"})
    assert await store.delete_run("queued") is False
    assert await store.delete_run("queued", allow_unstarted=True) is True

    await store.create_run("started")
    await store.emit("started", "runtime", "run.started", {"status": "running"})
    assert await store.delete_run("started", allow_unstarted=True) is False


@pytest.mark.asyncio
async def test_stream_rejects_an_after_sequence_ahead_of_current_history() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    await store.emit_terminal(
        "run-1",
        {"final_status": "internal_error", "stop_reason": "internal_error"},
    )

    with pytest.raises(EventCursorAhead):
        await anext(store.stream("run-1", after_sequence=2))


@pytest.mark.asyncio
async def test_closing_a_stream_releases_the_active_delete_guard() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    await store.emit("run-1", "runtime", "run.created", {"status": "queued"})
    stream = store.stream("run-1", after_sequence=None)
    await anext(stream)
    assert await store.delete_run("run-1", allow_unstarted=True) is False

    await stream.aclose()

    assert await store.delete_run("run-1", allow_unstarted=True) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("node,event_type,data", VALID_EVENTS)
async def test_all_wire_event_shapes_are_accepted(
    node: str, event_type: str, data: dict[str, Any]
) -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")

    event = await emit_valid(store, "run-1", node, event_type, data)

    assert event.node == node
    assert event.type == event_type


@pytest.mark.asyncio
@pytest.mark.parametrize("node,event_type,data", VALID_EVENTS)
async def test_every_wire_event_rejects_extra_keys(
    node: str, event_type: str, data: dict[str, Any]
) -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    malformed = {**data, "raw": "provider raw credential sentinel"}

    with pytest.raises(InvalidRunEvent):
        await emit_valid(store, "run-1", node, event_type, malformed)
    assert await store.high_water_mark("run-1") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node,event_type,data",
    (
        ("runtime", "run.created", {}),
        ("runtime", "run.created", {"status": {"raw": "sentinel"}}),
        (
            "retrieve_context",
            "context.retrieved",
            {"metric_count": True, "table_count": 1, "success": True},
        ),
        (
            "decide_behavior",
            "behavior.decided",
            {"action": "execute", "reason_code": "unsafe_request", "missing_fields": ()},
        ),
        (
            "build_plan",
            "plan.created",
            {
                "plan_id": "p",
                "revision": 1,
                "analysis_type": "simple",
                "metric_id": "gmv",
                "hypothesis_ids": ["h"],
            },
        ),
        (
            "invoke_tool",
            "tool.completed",
            {
                "tool_name": "execute_sql",
                "purpose": "p",
                "query_id": "a" * 64,
                "columns": ("secret_token",),
                "row_count": 1,
                "possibly_truncated": False,
            },
        ),
        (
            "route_action",
            "budget.warning",
            {
                "reason": "cost_soft_cap",
                "llm_calls": 1,
                "tool_calls": 1,
                "execute_calls": 1,
                "profile_calls": 0,
                "repair_count": 0,
                "committed_cost_cny": "NaN",
            },
        ),
        (
            "route_action",
            "budget.warning",
            {
                "reason": "cost_soft_cap",
                "llm_calls": 1,
                "tool_calls": 1,
                "execute_calls": 1,
                "profile_calls": 1,
                "repair_count": 0,
                "committed_cost_cny": "0.20",
            },
        ),
    ),
)
async def test_malformed_types_nested_payloads_and_sensitive_identifiers_are_rejected(
    node: str, event_type: str, data: dict[str, object]
) -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    with pytest.raises(InvalidRunEvent):
        await store.emit("run-1", node, event_type, data)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_event_node_ownership_and_terminal_entrypoint_are_enforced() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    with pytest.raises(InvalidRunEvent):
        await store.emit(
            "run-1",
            "repair",
            "behavior.decided",
            {"action": "execute", "reason_code": "ready", "missing_fields": ()},
        )
    with pytest.raises(InvalidRunEvent):
        await store.emit(
            "run-1",
            "runtime",
            "run.terminal",
            {"final_status": "completed", "stop_reason": "answer_complete"},
        )
    with pytest.raises(InvalidRunEvent):
        await store.emit_terminal(
            "run-1",
            {"final_status": "completed", "stop_reason": "answer_complete", "rows": ()},
        )


@pytest.mark.asyncio
async def test_terminal_status_and_stop_reason_must_match() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    with pytest.raises(InvalidRunEvent):
        await store.emit_terminal(
            "run-1",
            {"final_status": "completed", "stop_reason": "internal_error"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["sql_timeout", "task_timeout"])
@pytest.mark.parametrize("final_status", ["execution_failed", "partial"])
async def test_terminal_timeout_statuses_preserve_the_timeout_reason(
    final_status: str,
    reason: str,
) -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")

    event = await store.emit_terminal(
        "run-1", {"final_status": final_status, "stop_reason": reason}
    )

    assert event.data == MappingProxyType({"final_status": final_status, "stop_reason": reason})


@pytest.mark.asyncio
async def test_terminal_task_timeout_rejects_budget_exhausted() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    with pytest.raises(InvalidRunEvent):
        await store.emit_terminal(
            "run-1",
            {"final_status": "budget_exhausted", "stop_reason": "task_timeout"},
        )


@pytest.mark.asyncio
async def test_task7_safe_raw_sql_identifiers_cross_the_bound_sink() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    sink = BoundEventSink(store, "run-1")

    await sink.emit(
        "build_plan",
        "plan.created",
        {
            "plan_id": "sql_plan",
            "revision": 1,
            "analysis_type": "simple",
            "metric_id": "provider_metric",
            "hypothesis_ids": ("raw_hypothesis",),
        },
    )
    await sink.emit(
        "invoke_tool",
        "tool.completed",
        {
            "tool_name": "execute_sql",
            "purpose": "credential_contract",
            "query_id": "a" * 64,
            "columns": ("raw_value", "parameter_count"),
            "row_count": 1,
            "possibly_truncated": False,
        },
    )
    await store.emit_terminal(
        "run-1", {"final_status": "completed", "stop_reason": "answer_complete"}
    )
    events = [event async for event in store.stream("run-1", after_sequence=0)]
    event = next(event for event in events if event.type == "tool.completed")

    assert event.data["columns"] == ("raw_value", "parameter_count")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node,event_type,data",
    (
        (
            "decide_behavior",
            "behavior.decided",
            {
                "action": "clarify",
                "reason_code": "missing_time_window",
                "missing_fields": ("time_window",),
            },
        ),
        (
            "repair",
            "tool.started",
            {
                "tool_name": "execute_sql",
                "purpose": "metric_value_contract",
                "contract_id": "metric_value_contract",
            },
        ),
        (
            "invoke_tool",
            "tool.completed",
            {
                "tool_name": "profile",
                "purpose": "profile_context",
                "query_id": "b" * 64,
                "columns": ("value", "value_count"),
                "row_count": 2,
                "possibly_truncated": False,
            },
        ),
        (
            "repair",
            "tool.failed",
            {
                "tool_name": "execute_sql",
                "purpose": "metric_value_contract",
                "safe_error": "internal_tool_error",
            },
        ),
        (
            "invoke_tool",
            "tool.completed",
            {
                "tool_name": "profile",
                "purpose": "profile_context",
                "query_id": "c" * 64,
                "columns": ("null_count", "row_count"),
                "row_count": 1,
                "possibly_truncated": False,
            },
        ),
        (
            "validate_observation",
            "observation.validated",
            {
                "contract_id": "metric_value_contract",
                "valid": False,
                "error_code": "column_contract_mismatch",
                "repairable": True,
            },
        ),
        (
            "judge_evidence",
            "evidence.assessed",
            {
                "verified_count": 1,
                "gaps": ("region_contribution",),
                "partial": True,
                "complete": False,
            },
        ),
    ),
)
async def test_task7_builder_variants_are_accepted(
    node: str, event_type: str, data: dict[str, Any]
) -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    await store.emit("run-1", node, event_type, data)


@pytest.mark.asyncio
async def test_run_event_json_envelope_is_safe_and_stable() -> None:
    store = InMemoryEventStore(clock=lambda: datetime(2026, 9, 4, tzinfo=UTC))
    await store.create_run("run-1")
    event = await store.emit(
        "run-1",
        "decide_behavior",
        "behavior.decided",
        {"action": "execute", "reason_code": "ready", "missing_fields": ()},
    )

    envelope = json.loads(event.model_dump_json())

    assert envelope == {
        "event_id": "run-1:1",
        "sequence": 1,
        "run_id": "run-1",
        "timestamp": "2026-09-04T00:00:00Z",
        "node": "decide_behavior",
        "type": "behavior.decided",
        "data": {"action": "execute", "reason_code": "ready", "missing_fields": []},
    }


@pytest.mark.asyncio
async def test_event_data_is_an_owned_frozen_snapshot_and_history_is_immutable() -> None:
    store = InMemoryEventStore()
    await store.create_run("run-1")
    source: dict[str, object] = {
        "action": "execute",
        "reason_code": "ready",
        "missing_fields": (),
    }
    event = await store.emit(
        "run-1",
        "decide_behavior",
        "behavior.decided",
        source,  # type: ignore[arg-type]
    )
    source["action"] = "refuse"
    source["raw"] = "credential sentinel"

    stream = store.stream("run-1", after_sequence=None)
    replayed = await anext(stream)
    assert replayed is event
    assert event.data == MappingProxyType(
        {"action": "execute", "reason_code": "ready", "missing_fields": ()}
    )
    with pytest.raises(TypeError):
        event.data["action"] = "refuse"  # type: ignore[index]
    with pytest.raises(ValidationError):
        event.sequence = 99
    await stream.aclose()
