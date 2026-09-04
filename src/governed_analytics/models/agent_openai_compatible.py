"""OpenAI-compatible adapter for the independent Agent structured-model boundary."""

from __future__ import annotations

import re
from contextlib import suppress
from time import monotonic
from typing import TypedDict

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from governed_analytics.agent.contracts import (
    AgentFinishReason,
    ModelUsage,
    StructuredModelRequest,
    StructuredModelResult,
)
from governed_analytics.agent.ports import AgentModelError

_SAFE_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _ErrorMetadata(TypedDict):
    provider_model: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    finish_reason: AgentFinishReason | None
    output_truncated: bool


def _safe_model_identifier(value: object) -> str | None:
    if (
        isinstance(value, str)
        and _SAFE_MODEL_IDENTIFIER.fullmatch(value) is not None
        and not value.lower().startswith(("sk-", "pk-", "bearer-"))
    ):
        return value
    return None


def _strict_positive_integer(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _normalise_finish_reason(value: object) -> AgentFinishReason | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return AgentFinishReason.UNKNOWN
    known = {
        "stop": AgentFinishReason.STOP,
        "length": AgentFinishReason.LENGTH,
        "content_filter": AgentFinishReason.CONTENT_FILTER,
        "tool_calls": AgentFinishReason.TOOL_CALLS,
        "function_call": AgentFinishReason.OTHER,
    }
    return known.get(value, AgentFinishReason.UNKNOWN)


def _validation_category(error: ValidationError) -> str:
    error_types = {
        item.get("type")
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    }
    return "invalid_json" if "json_invalid" in error_types else "invalid_structure"


class OpenAICompatibleAgentModel:
    """Validate one provider response directly into the requested Agent contract."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        safe_model = _safe_model_identifier(model)
        if safe_model is None:
            raise AgentModelError("invalid_request")
        self._client = client
        self._model = safe_model

    @property
    def model(self) -> str:
        return self._model

    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredModelResult[T]:
        if request.output_schema_name != output_type.__name__:
            raise AgentModelError("schema_identity_mismatch")

        started_at = monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_json()},
                ],
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=request.max_output_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception:
            latency_ms = max(0, round((monotonic() - started_at) * 1000))
            raise AgentModelError("provider_call_failed", latency_ms=latency_ms) from None
        latency_ms = max(0, round((monotonic() - started_at) * 1000))

        provider_model_value: object = None
        with suppress(Exception):
            provider_model_value = response.model
        provider_model = _safe_model_identifier(provider_model_value)

        usage: object = None
        usage_accessible = False
        usage_fields_accessible = False
        input_tokens: int | None = None
        output_tokens: int | None = None
        with suppress(Exception):
            usage = response.usage
            usage_accessible = True
        if usage is not None:
            with suppress(Exception):
                input_tokens = _strict_positive_integer(usage.prompt_tokens)  # type: ignore[attr-defined]
                output_tokens = _strict_positive_integer(usage.completion_tokens)  # type: ignore[attr-defined]
                usage_fields_accessible = True

        metadata: _ErrorMetadata = {
            "provider_model": provider_model,
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "latency_ms": latency_ms,
            "finish_reason": None,
            "output_truncated": False,
        }

        try:
            choice = response.choices[0]
        except Exception:
            raise AgentModelError("missing_content", **metadata) from None
        raw_finish_reason: object = None
        with suppress(Exception):
            raw_finish_reason = choice.finish_reason
        finish_reason = _normalise_finish_reason(raw_finish_reason)
        metadata["finish_reason"] = finish_reason
        metadata["output_truncated"] = finish_reason is AgentFinishReason.LENGTH
        try:
            content = choice.message.content
        except Exception:
            raise AgentModelError("missing_content", **metadata) from None
        if content is None:
            raise AgentModelError("missing_content", **metadata)
        if not isinstance(content, str):
            raise AgentModelError("invalid_content_type", **metadata)
        try:
            output = output_type.model_validate_json(content)
        except ValidationError as error:
            raise AgentModelError(_validation_category(error), **metadata) from None

        if not usage_accessible or usage is None or not usage_fields_accessible:
            raise AgentModelError("missing_usage", **metadata)
        if input_tokens is None or output_tokens is None:
            raise AgentModelError("invalid_usage", **metadata)
        if provider_model is None:
            raise AgentModelError("missing_model", **metadata)
        return StructuredModelResult(
            output=output,
            provider_model=provider_model,
            usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            output_truncated=finish_reason is AgentFinishReason.LENGTH,
        )


__all__ = ["OpenAICompatibleAgentModel"]
