from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from governed_analytics.config import ModelSettings
from governed_analytics.models.openai_compatible import (
    ModelAdapterError,
    OpenAICompatibleSqlGenerator,
)
from governed_analytics.models.prompts import (
    BASELINE_SYSTEM_PROMPT_V2,
    build_baseline_user_prompt,
)
from governed_analytics.models.protocols import SqlGenerationRequest, SqlGenerator


def _request() -> SqlGenerationRequest:
    return SqlGenerationRequest(
        case_id="G001",
        question="上周 GMV 是多少?",
        schema_context="orders(order_id, status)",
        metric_context="gmv = sum(net_amount)",
    )


def _response(
    *,
    content: object = '{"sql":"select 1 as value","assumptions":["valid orders only"]}',
    model: object = "deepseek-v4-flash",
    prompt_tokens: object = 17,
    completion_tokens: object = 9,
    finish_reason: object = None,
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


@pytest.mark.asyncio
async def test_live_adapter_sends_complete_one_pass_contract_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_response())
    generator: SqlGenerator = OpenAICompatibleSqlGenerator(
        cast(AsyncOpenAI, client), "deepseek-v4-flash"
    )
    moments = iter((100.0, 101.234))
    monkeypatch.setattr(
        "governed_analytics.models.openai_compatible.monotonic", lambda: next(moments)
    )

    result = await generator.generate(_request())

    assert result.sql == "select 1 as value"
    assert result.assumptions == ("valid orders only",)
    assert result.provider_model == "deepseek-v4-flash"
    assert result.input_tokens == 17
    assert result.output_tokens == 9
    assert result.latency_ms == 1234
    assert result.finish_reason is None
    assert result.output_truncated is False
    assert client.completions.calls == [
        {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": BASELINE_SYSTEM_PROMPT_V2},
                {"role": "user", "content": build_baseline_user_prompt(_request())},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": 1200,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    ]


@pytest.mark.asyncio
async def test_live_adapter_makes_exactly_one_call_and_sanitizes_sdk_failures() -> None:
    client = _FakeClient(RuntimeError("https://endpoint.example/key=secret/raw provider failure"))
    generator = OpenAICompatibleSqlGenerator(client, "deepseek-v4-flash")  # type: ignore[arg-type]

    with pytest.raises(ModelAdapterError, match=r"^provider_call_failed$") as error:
        await generator.generate(_request())

    assert error.value.__cause__ is None
    assert "endpoint" not in str(error.value)
    assert "secret" not in str(error.value)
    assert len(client.completions.calls) == 1


@pytest.mark.asyncio
async def test_invalid_assumptions_preserves_safe_call_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_response(content='{"sql":"select 1","assumptions":"none"}'))
    generator = OpenAICompatibleSqlGenerator(client, "deepseek-v4-flash")  # type: ignore[arg-type]
    moments = iter((100.0, 100.25))
    monkeypatch.setattr(
        "governed_analytics.models.openai_compatible.monotonic", lambda: next(moments)
    )

    with pytest.raises(ModelAdapterError, match=r"^invalid_assumptions$") as captured:
        await generator.generate(_request())

    error = captured.value
    assert error.provider_model == "deepseek-v4-flash"
    assert error.input_tokens == 17
    assert error.output_tokens == 9
    assert error.latency_ms == 250
    assert "select 1" not in repr(error)


@pytest.mark.asyncio
async def test_live_adapter_preserves_safe_length_telemetry_when_json_is_truncated() -> None:
    raw_content = '{"sql":"select 1 as value","assumptions":['
    client = _FakeClient(_response(content=raw_content, finish_reason="length"))
    generator = OpenAICompatibleSqlGenerator(client, "deepseek-v4-flash")  # type: ignore[arg-type]

    with pytest.raises(ModelAdapterError, match=r"^invalid_json$") as captured:
        await generator.generate(_request())

    error = captured.value
    assert error.finish_reason == "length"
    assert error.output_truncated is True
    assert raw_content not in str(error)
    assert raw_content not in repr(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_reason", "expected"),
    [
        ("stop", "stop"),
        ("content_filter", "content_filter"),
        ("tool_calls", "tool_calls"),
        ("function_call", "other"),
        ("Length", "unknown"),
        ("provider-secret-reason", "unknown"),
        (7, "unknown"),
    ],
)
async def test_live_adapter_normalises_finish_reason_without_leaking_provider_values(
    raw_reason: object, expected: str
) -> None:
    generator = OpenAICompatibleSqlGenerator(
        cast(AsyncOpenAI, _FakeClient(_response(finish_reason=raw_reason))),
        "deepseek-v4-flash",
    )

    result = await generator.generate(_request())

    assert result.finish_reason == expected
    assert result.output_truncated is False
    assert "provider-secret-reason" not in repr(result)


@pytest.mark.asyncio
async def test_live_adapter_marks_length_as_truncated_even_when_json_is_complete() -> None:
    generator = OpenAICompatibleSqlGenerator(
        cast(AsyncOpenAI, _FakeClient(_response(finish_reason="length"))),
        "deepseek-v4-flash",
    )

    result = await generator.generate(_request())

    assert result.finish_reason == "length"
    assert result.output_truncated is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "category"),
    [
        (SimpleNamespace(choices=[]), "missing_content"),
        (_response(content=None), "missing_content"),
        (_response(content=1), "invalid_content_type"),
        (_response(content="not JSON"), "invalid_json"),
        (_response(content='{"sql":"select 1","assumptions":[],"extra":true}'), "invalid_envelope"),
        (_response(content='{"sql":" select 1","assumptions":[]}'), "invalid_sql_content"),
        (_response(content='{"sql":"delete from orders","assumptions":[]}'), "invalid_sql_content"),
        (
            _response(content='{"sql":"selective_not_a_query","assumptions":[]}'),
            "invalid_sql_content",
        ),
        (
            _response(content='{"sql":"withholding_not_a_query","assumptions":[]}'),
            "invalid_sql_content",
        ),
        (
            _response(content='{"sql":"SELECTive_not_a_query","assumptions":[]}'),
            "invalid_sql_content",
        ),
        (_response(content='{"sql":"select 1","assumptions":[1]}'), "invalid_assumptions"),
        (_response(content='{"sql":"select 1","assumptions":[" "]}'), "invalid_assumptions"),
        (
            _response(content=json.dumps({"sql": "select 1", "assumptions": ["x" * 201]})),
            "invalid_assumptions",
        ),
        (
            _response(content=json.dumps({"sql": "select 1", "assumptions": ["x"] * 9})),
            "invalid_assumptions",
        ),
        (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"sql":"select 1","assumptions":[]}')
                    )
                ],
                model="model",
            ),
            "missing_usage",
        ),
        (_response(prompt_tokens=0), "invalid_usage"),
        (_response(completion_tokens=True), "invalid_usage"),
        (_response(model=" "), "missing_model"),
        (_response(model="provider-model\nsecret-marker"), "missing_model"),
        (_response(model="sk-0123456789abcdef0123456789abcdef"), "missing_model"),
    ],
)
async def test_live_adapter_classifies_invalid_provider_responses(
    response: object, category: str
) -> None:
    generator = OpenAICompatibleSqlGenerator(_FakeClient(response), "deepseek-v4-flash")  # type: ignore[arg-type]

    with pytest.raises(ModelAdapterError, match=rf"^{category}$") as error:
        await generator.generate(_request())

    assert error.value.__cause__ is None
    assert "select 1" not in str(error.value)
    assert "secret-marker" not in str(error.value)


@pytest.mark.parametrize(
    "model",
    ("", " \t ", 1, b"deepseek-v4-flash", "sk-0123456789abcdef0123456789abcdef"),
)
def test_live_adapter_rejects_invalid_requested_model(model: object) -> None:
    with pytest.raises(ModelAdapterError, match=r"^invalid_request$"):
        OpenAICompatibleSqlGenerator(object(), model)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "base_url",
    (
        "http://provider.example/v1",
        "https://user:password@provider.example/v1",
        "https://provider.example/v1?x=1",
        "https://provider.example/v1#fragment",
        "https:///v1",
    ),
)
def test_model_settings_reject_invalid_or_credentialed_urls(base_url: str) -> None:
    with pytest.raises(ValidationError):
        ModelSettings(model_base_url=base_url, _env_file=None)  # type: ignore[call-arg]


def test_model_settings_do_not_echo_rejected_url_credentials() -> None:
    username = "model-user"
    password = "leaked-password"
    rejected_url = f"https://{username}:{password}@provider.example/v1"

    with pytest.raises(ValidationError) as error:
        ModelSettings(model_base_url=rejected_url, _env_file=None)  # type: ignore[call-arg]

    public_error = str(error.value)
    assert username not in public_error
    assert password not in public_error
    assert rejected_url not in public_error
    assert username not in str(error.value.errors())
    assert password not in error.value.json()


def test_model_settings_are_frozen_and_allow_local_http_without_unwrapping_secret() -> None:
    defaults = ModelSettings(_env_file=None)  # type: ignore[call-arg]
    settings = ModelSettings(
        model_base_url="http://127.0.0.1:8000/v1",
        model_api_key=SecretStr("never-unwrap-this"),
        _env_file=None,
    )  # type: ignore[call-arg]

    assert defaults.model_base_url == "https://api.deepseek.com"
    assert defaults.model_name == "deepseek-v4-flash"
    assert defaults.eval_model_name == "DeepSeek-V4-Flash-0731"
    assert settings.model_base_url == "http://127.0.0.1:8000/v1"
    assert "never-unwrap-this" not in repr(settings)
    with pytest.raises(ValidationError):
        settings.model_name = "other"
