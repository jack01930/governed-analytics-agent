from __future__ import annotations

import json
from collections.abc import Iterator
from functools import partial
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

import governed_analytics.api.dependencies as dependencies
from governed_analytics.api.app import create_app
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.models.agent_fixtures import AgentScripts
from governed_analytics.persistence.database import create_async_database_engine

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
    {"sql", "rows", "credentials", "token", "prompt", "payload", "parameters"}
)


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


class _SpyEngine:
    def __init__(self, delegate: AsyncEngine) -> None:
        self._delegate = delegate
        self.connect_calls = 0
        self.dispose_calls = 0

    def connect(self):  # type: ignore[no-untyped-def]
        self.connect_calls += 1
        return self._delegate.connect()

    async def dispose(self) -> None:
        self.dispose_calls += 1
        await self._delegate.dispose()


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
        assert not (_FORBIDDEN_KEYS & {str(key).casefold() for key in value})
        for item in value.values():
            _assert_safe_tree(item)
    elif isinstance(value, list):
        for item in value:
            _assert_safe_tree(item)
    elif isinstance(value, str):
        lowered = value.casefold()
        assert "select " not in lowered
        assert ":previous_start" not in lowered
        assert "postgresql+" not in lowered


@pytest.mark.integration
def test_post_sse_status_and_trace_share_one_lifespan_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_factory = create_async_database_engine
    engines: list[_SpyEngine] = []

    def engine_factory(settings: DatabaseSettings) -> _SpyEngine:
        engine = _SpyEngine(real_factory(settings))
        engines.append(engine)
        return engine

    monkeypatch.setattr(dependencies, "create_async_database_engine", engine_factory)
    monkeypatch.setattr(dependencies, "builtin_demo_scripts", lambda: API_SCRIPTS)
    runtime = AgentRuntimeSettings(_env_file=None)  # type: ignore[call-arg]
    database = DatabaseSettings()  # type: ignore[call-arg]
    model = ModelSettings(_env_file=None)  # type: ignore[call-arg]
    app = create_app(
        container_factory=partial(
            dependencies.build_container,
            runtime_settings=runtime,
            database_settings=database,
            model_settings=model,
        )
    )

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
        assert sum(
            tool["tool_name"] == "execute_sql" for tool in trace_body["tool_calls"]
        ) == 4
        assert engines[0].connect_calls == 4
        assert engines[0].dispose_calls == 0
        _assert_safe_tree(created_body)
        _assert_safe_tree(list(events))
        _assert_safe_tree(status_body)
        _assert_safe_tree(trace_body)

    assert len(engines) == 1
    assert engines[0].dispose_calls == 1
