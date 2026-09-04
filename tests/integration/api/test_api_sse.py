from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import cast
from urllib.parse import unquote, urlsplit

import pytest
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
_CREDENTIAL_CANARY = "task11-fix1-credential-canary-never-expose"


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
        await execution_gate.wait()
        await live_read_gate.wait()
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
        if callable(close):
            await close()
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


def _sse_payloads(lines: Iterator[str]) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    protocol_id: str | None = None
    for line in lines:
        if line.startswith("id: "):
            protocol_id = line.removeprefix("id: ")
        elif line.startswith("data: "):
            payload = cast(dict[str, object], json.loads(line.removeprefix("data: ")))
            assert payload["event_id"] == protocol_id
            payloads.append(payload)
    return tuple(payloads)


def _assert_safe_tree(value: object) -> None:
    if isinstance(value, dict):
        normalized_keys = {str(key).casefold().replace("-", "_") for key in value}
        assert not (_FORBIDDEN_KEYS & normalized_keys)
        assert not {
            key for key in normalized_keys if key.endswith("_sql") or key.startswith("sql_")
        }
        for item in value.values():
            _assert_safe_tree(item)
    elif isinstance(value, list):
        for item in value:
            _assert_safe_tree(item)
    elif isinstance(value, str):
        lowered = value.casefold()
        assert re.search(r"\b(?:select|with)\s", lowered) is None
        assert ":previous_start" not in lowered
        assert "postgresql+" not in lowered


def _assert_exact_keys(value: object, allowed: frozenset[str]) -> Mapping[str, object]:
    assert isinstance(value, dict)
    assert frozenset(map(str, value)) == allowed
    return value


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
        assert isinstance(event_type, str)
        assert event_type in _EVENT_DATA_KEYS
        _assert_exact_keys(envelope["data"], _EVENT_DATA_KEYS[event_type])

    status_mapping = _assert_exact_keys(status, _STATUS_KEYS)
    evidence = status_mapping["evidence"]
    assert isinstance(evidence, list)
    for item in evidence:
        _assert_exact_keys(item, _EVIDENCE_KEYS)

    trace_mapping = _assert_exact_keys(trace, _TRACE_KEYS)
    nodes = trace_mapping["nodes"]
    model_calls = trace_mapping["model_calls"]
    tool_calls = trace_mapping["tool_calls"]
    assert isinstance(nodes, list)
    assert isinstance(model_calls, list)
    assert isinstance(tool_calls, list)
    for item in nodes:
        _assert_exact_keys(item, _NODE_TRACE_KEYS)
    for item in model_calls:
        _assert_exact_keys(item, _MODEL_TRACE_KEYS)
    for item in tool_calls:
        _assert_exact_keys(item, _TOOL_TRACE_KEYS)


def _assert_serialized_output_has_no_secrets_or_sql(
    wire_text: str,
    database: DatabaseSettings,
) -> None:
    parsed_database_url = urlsplit(database.database_url)
    username = unquote(parsed_database_url.username or "")
    password = unquote(parsed_database_url.password or "")
    assert username and password
    for secret in (
        database.database_url,
        username,
        password,
        _CREDENTIAL_CANARY,
    ):
        assert secret not in wire_text

    normalized_wire = " ".join(wire_text.casefold().split())
    for sql in (_COMPARISON_SQL, _REGION_SQL, _SKU_SQL, _SEGMENT_SQL):
        assert " ".join(sql.casefold().split()) not in normalized_wire
    assert re.search(r"\b(?:select|with)\s", normalized_wire) is None


@pytest.mark.integration
def test_post_sse_status_and_trace_share_one_lifespan_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AssertionError):
        _assert_safe_tree({"raw_sql": "WITH\nsecret_cte AS (VALUES (1))"})
    _assert_safe_tree({"result_summary": "contracted aggregate only"})

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
        created = client.post("/v1/analyses", json={"query": ATTRIBUTION_QUERY})
        assert created.status_code == 202
        created_body = created.json()
        assert len(engines) == 1
        assert engines[0].dispose_calls == 0

        with client.stream("GET", created_body["events_url"]) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = _sse_payloads(response.iter_lines())

        status = client.get(created_body["status_url"])
        trace = client.get(created_body["trace_url"])
        assert status.status_code == trace.status_code == 200
        status_body = status.json()
        trace_body = trace.json()

        sequences = tuple(cast(int, event["sequence"]) for event in events)
        terminal = tuple(event for event in events if event["type"] == "run.terminal")
        assert sequences == tuple(range(1, len(events) + 1))
        assert len(terminal) == 1
        assert events[-1] == terminal[0]
        assert status_body["lifecycle_status"] == "terminal"
        assert status_body["final_status"] == "completed"
        assert status_body["stop_reason"] == "answer_complete"
        assert trace_body["snapshot_complete"] is True
        assert trace_body["stop_reason"] == "answer_complete"
        assert trace_body["tool_call_count"] == 6
        assert sum(tool["tool_name"] == "execute_sql" for tool in trace_body["tool_calls"]) == 4
        assert engines[0].connect_calls == 4
        assert engines[0].dispose_calls == 0
        assert probe.open_nonterminal is True
        assert probe.open_high_water >= 1
        assert probe.live_wait_started is True
        assert any(sequence > probe.open_high_water for sequence in sequences)
        assert probe.active_leases == 0

        event_store = probe.event_store
        portal = client.portal
        assert event_store is not None
        assert portal is not None
        assert portal.call(event_store.delete_run, created_body["run_id"]) is True

        wire = {
            "created": created_body,
            "events": list(events),
            "status": status_body,
            "trace": trace_body,
        }
        _assert_wire_allowlists(created_body, events, status_body, trace_body)
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
