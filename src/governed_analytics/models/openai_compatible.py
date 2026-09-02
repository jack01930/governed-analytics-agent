"""One-pass OpenAI-compatible live SQL adapter with a sanitized error boundary."""

from __future__ import annotations

import json
from time import monotonic

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from governed_analytics.evals.models import GeneratedSql
from governed_analytics.models.prompts import BASELINE_SYSTEM_PROMPT_V1, build_baseline_user_prompt
from governed_analytics.models.protocols import SqlGenerationRequest


class ModelAdapterError(ValueError):
    """Stable public model-adapter error containing a safe category only."""


class _ProviderSqlResponse(BaseModel):
    """Strict, intentionally private provider JSON envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sql: StrictStr
    assumptions: list[StrictStr]

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, sql: str) -> str:
        if not sql.strip() or not sql.lower().startswith(("select", "with")):
            raise ValueError("invalid SQL")
        return sql

    @field_validator("assumptions")
    @classmethod
    def validate_assumptions(cls, assumptions: list[str]) -> list[str]:
        if len(assumptions) > 8 or any(not item.strip() or len(item) > 200 for item in assumptions):
            raise ValueError("invalid assumptions")
        return assumptions


def _error(category: str) -> ModelAdapterError:
    return ModelAdapterError(category)


def _strict_positive_integer(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


class OpenAICompatibleSqlGenerator:
    """Generate one structured SQL answer through an already-created SDK client."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise _error("invalid_request")
        self._client = client
        self._model = model

    async def generate(self, request: SqlGenerationRequest) -> GeneratedSql:
        started_at = monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": BASELINE_SYSTEM_PROMPT_V1},
                    {"role": "user", "content": build_baseline_user_prompt(request)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
                max_completion_tokens=1200,
                extra_body={"enable_thinking": False},
            )
        except Exception:
            raise _error("provider_call_failed") from None
        latency_ms = max(0, round((monotonic() - started_at) * 1000))

        try:
            content = response.choices[0].message.content
        except Exception:
            raise _error("missing_content") from None
        if content is None:
            raise _error("missing_content")
        if not isinstance(content, str):
            raise _error("invalid_content")
        try:
            parsed_content = _ProviderSqlResponse.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            raise _error("invalid_content") from None

        try:
            usage = response.usage
        except Exception:
            raise _error("missing_usage") from None
        if usage is None:
            raise _error("missing_usage")
        try:
            input_tokens = _strict_positive_integer(usage.prompt_tokens)
            output_tokens = _strict_positive_integer(usage.completion_tokens)
        except Exception:
            raise _error("missing_usage") from None
        if input_tokens is None or output_tokens is None:
            raise _error("invalid_usage")

        try:
            provider_model = response.model
        except Exception:
            raise _error("missing_model") from None
        if not isinstance(provider_model, str) or not provider_model.strip():
            raise _error("missing_model")
        return GeneratedSql(
            sql=parsed_content.sql,
            assumptions=tuple(parsed_content.assumptions),
            provider_model=provider_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )
