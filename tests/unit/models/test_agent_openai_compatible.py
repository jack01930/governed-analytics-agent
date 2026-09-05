from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import httpx2 as httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from governed_analytics.agent.contracts import (
    AnalysisAction,
    BehaviorDecision,
    FinalAnswer,
    StructuredModelRequest,
    TypedMetricPlan,
)
from governed_analytics.agent.ports import AgentModelError
from governed_analytics.models.agent_openai_compatible import OpenAICompatibleAgentModel


class _FakeCompletions:
    def __init__(self, response: object | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _FakeClient:
    def __init__(self, response: object | Exception) -> None:
        self.completions = _FakeCompletions(response)
        self.chat = SimpleNamespace(completions=self.completions)


def _response(
    *,
    content: object,
    model: object = "DeepSeek-V4-Flash-0731",
    prompt_tokens: object = 12,
    completion_tokens: object = 8,
    finish_reason: object = "stop",
) -> object:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
        ],
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _request(*, schema_name: str = "BehaviorDecision") -> StructuredModelRequest:
    return StructuredModelRequest(
        purpose="behavior",
        system_prompt="只返回契约 JSON。",
        user_payload={"query": "预测明年GMV"},
        output_schema_name=schema_name,
        output_schema_summary={"title": schema_name, "type": "object"},
        max_output_tokens=300,
    )


@pytest.mark.asyncio
async def test_agent_adapter_requests_json_without_hidden_thinking() -> None:
    client = _FakeClient(
        _response(
            content=(
                '{"action":"unsupported","reason_code":"unsupported_analysis",'
                '"missing_fields":[],"user_message":"暂不支持该分析。"}'
            )
        )
    )
    model = OpenAICompatibleAgentModel(cast(AsyncOpenAI, client), "deepseek-v4-flash")

    result = await model.invoke(_request(), BehaviorDecision)

    assert result.output.action == "unsupported"
    assert result.provider_model == "DeepSeek-V4-Flash-0731"
    assert result.input_tokens == 12
    assert result.output_tokens == 8
    assert client.completions.calls == [
        {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": _request().provider_system_prompt()},
                {"role": "user", "content": '{"query":"预测明年GMV"}'},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": 300,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    ]


@pytest.mark.asyncio
async def test_schema_identity_mismatch_never_calls_provider() -> None:
    client = _FakeClient(RuntimeError("must not be called"))
    model = OpenAICompatibleAgentModel(cast(AsyncOpenAI, client), "deepseek-v4-flash")

    with pytest.raises(AgentModelError, match=r"^schema_identity_mismatch$"):
        await model.invoke(_request(schema_name="OtherDecision"), BehaviorDecision)

    assert client.completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "category"),
    [
        ("not-json", "invalid_json"),
        ('{"action":"execute"}', "invalid_structure"),
    ],
)
async def test_agent_adapter_sanitizes_invalid_structured_content(
    content: str, category: str
) -> None:
    client = _FakeClient(_response(content=content))
    model = OpenAICompatibleAgentModel(cast(AsyncOpenAI, client), "deepseek-v4-flash")

    with pytest.raises(AgentModelError, match=rf"^{category}$") as raised:
        await model.invoke(_request(), BehaviorDecision)

    assert content not in repr(raised.value)
    assert raised.value.provider_model == "DeepSeek-V4-Flash-0731"
    assert raised.value.input_tokens == 12
    assert raised.value.output_tokens == 8


@pytest.mark.asyncio
async def test_agent_adapter_sanitizes_sdk_failures() -> None:
    raw_error = "https://secret.example/key=sk-secret raw prompt and response"
    client = _FakeClient(RuntimeError(raw_error))
    model = OpenAICompatibleAgentModel(cast(AsyncOpenAI, client), "deepseek-v4-flash")

    with pytest.raises(AgentModelError, match=r"^provider_call_failed$") as raised:
        await model.invoke(_request(), BehaviorDecision)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raw_error not in repr(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "category"),
    [
        (400, "provider_http_4xx"),
        (401, "provider_http_4xx"),
        (429, "provider_rate_limited"),
        (503, "provider_http_5xx"),
        ("timeout", "provider_timeout"),
        ("connection", "provider_connection_error"),
    ],
)
async def test_provider_failure_preserves_only_safe_category(
    kind: int | str, category: str
) -> None:
    transport_request = httpx.Request("POST", "https://example.invalid/private")
    if kind == "timeout":
        error: Exception = APITimeoutError(request=transport_request)
    elif kind == "connection":
        error = APIConnectionError(request=transport_request)
    else:
        error = APIStatusError(
            "SENSITIVE_ERROR_SENTINEL",
            response=httpx.Response(int(kind), request=transport_request),
            body={"private": "SENSITIVE_BODY_SENTINEL"},
        )
    client = _FakeClient(error)
    model = OpenAICompatibleAgentModel(cast(AsyncOpenAI, client), "deepseek-v4-flash")
    with pytest.raises(AgentModelError) as raised:
        await model.invoke(_request(), BehaviorDecision)
    assert raised.value.category.value == category
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "SENSITIVE" not in repr(vars(raised.value))
    assert len(client.completions.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_type", [BehaviorDecision, TypedMetricPlan, AnalysisAction, FinalAnswer]
)
@pytest.mark.parametrize("repair", [False, True])
async def test_production_requests_send_json_instruction_and_complete_schema(
    output_type: Any, repair: bool
) -> None:
    request = StructuredModelRequest.for_output(
        purpose="behavior",
        system_prompt="Classify the governed analytics request. Return only the bound schema.",
        user_payload={"query": "六月GMV"},
        output_type=output_type,
        max_output_tokens=300,
    )
    if repair:
        request = request.for_repair(failure_category="invalid_structure")
    client = _FakeClient(RuntimeError("test transport ends after recording the request"))
    model = OpenAICompatibleAgentModel(cast(AsyncOpenAI, client), "deepseek-v4-flash")
    with pytest.raises(AgentModelError):
        await model.invoke(request, output_type)
    sent = client.completions.calls[0]
    system = sent["messages"][0]["content"]
    assert "JSON" in system
    assert (
        json.loads(system.split("Output JSON Schema:\n", 1)[1]) == output_type.model_json_schema()
    )
    assert sent["response_format"] == {"type": "json_object"}
    budget_envelope = json.loads(request.prompt_bytes()[:-512])
    assert budget_envelope["messages"] == sent["messages"]
    assert budget_envelope["response_format"] == sent["response_format"]
