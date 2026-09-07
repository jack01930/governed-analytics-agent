from __future__ import annotations

import asyncio
from types import MappingProxyType

import pytest

from governed_analytics.agent.contracts import BehaviorDecision, StructuredModelRequest
from governed_analytics.agent.ports import AgentModelError
from governed_analytics.models.agent_fixtures import (
    AgentScripts,
    PurposeScripts,
    ScriptedAgentModel,
)


def _request(
    *, query: str = "六月GMV", schema_name: str = "BehaviorDecision"
) -> StructuredModelRequest:
    return StructuredModelRequest(
        purpose="behavior",
        system_prompt="contract",
        user_payload={"query": query},
        output_schema_name=schema_name,
        output_schema_summary={"title": schema_name, "type": "object"},
        max_output_tokens=300,
    )


def _scripts() -> AgentScripts:
    answer = MappingProxyType(
        {
            "action": "execute",
            "reason_code": "ready",
            "missing_fields": (),
            "user_message": "开始分析。",
        }
    )
    purposes: PurposeScripts = MappingProxyType({"behavior": (answer, answer)})
    return MappingProxyType({"六月gmv": purposes})


@pytest.mark.asyncio
async def test_scripted_model_consumes_exact_purpose_sequence() -> None:
    model = ScriptedAgentModel(_scripts())

    result = await model.invoke(_request(), BehaviorDecision)

    assert result.output.action == "execute"
    assert result.provider_model == "fixture-agent"
    assert (result.input_tokens, result.output_tokens) == (0, 0)


@pytest.mark.asyncio
async def test_two_sessions_for_same_script_have_independent_ordinals() -> None:
    scripts = _scripts()
    first = ScriptedAgentModel(scripts)
    second = ScriptedAgentModel(scripts)

    first_result = await first.invoke(_request(), BehaviorDecision)
    second_result = await second.invoke(_request(), BehaviorDecision)

    assert first_result.output == second_result.output


@pytest.mark.asyncio
async def test_two_concurrent_sessions_start_at_ordinal_one() -> None:
    scripts = _scripts()
    first, second = ScriptedAgentModel(scripts), ScriptedAgentModel(scripts)

    first_result, second_result = await asyncio.gather(
        first.invoke(_request(query="  六月GMV  "), BehaviorDecision),
        second.invoke(_request(query="六月gmv"), BehaviorDecision),
    )

    assert first_result.output == second_result.output


@pytest.mark.asyncio
async def test_fixture_schema_mismatch_happens_before_script_read() -> None:
    model = ScriptedAgentModel(_scripts())

    with pytest.raises(AgentModelError, match=r"^schema_identity_mismatch$"):
        await model.invoke(_request(schema_name="OtherDecision"), BehaviorDecision)

    result = await model.invoke(_request(), BehaviorDecision)
    assert result.output.action == "execute"


@pytest.mark.asyncio
async def test_fixture_missing_or_exhausted_script_is_safe() -> None:
    model = ScriptedAgentModel(_scripts())
    await model.invoke(_request(), BehaviorDecision)
    await model.invoke(_request(), BehaviorDecision)

    with pytest.raises(AgentModelError, match=r"^fixture_script_mismatch$") as raised:
        await model.invoke(_request(), BehaviorDecision)

    assert "六月" not in repr(raised.value)


@pytest.mark.asyncio
async def test_fixture_tracks_repair_ordinal_separately_from_behavior() -> None:
    behavior = {
        "action": "execute",
        "reason_code": "ready",
        "missing_fields": (),
        "user_message": "首次行为。",
    }
    repair = {
        "action": "execute",
        "reason_code": "ready",
        "missing_fields": (),
        "user_message": "首次修复。",
    }
    purposes: PurposeScripts = {"behavior": (behavior,), "repair": (repair,)}
    model = ScriptedAgentModel({"六月gmv": purposes})

    behavior_result = await model.invoke(_request(), BehaviorDecision)
    repair_result = await model.invoke(
        _request().for_repair(failure_category="invalid_structure"),
        BehaviorDecision,
    )

    assert behavior_result.output.user_message == "首次行为。"
    assert repair_result.output.user_message == "首次修复。"
