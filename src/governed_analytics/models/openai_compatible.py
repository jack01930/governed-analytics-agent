"""One-pass OpenAI-compatible live SQL adapter with a sanitized error boundary."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from time import monotonic
from typing import TypedDict

import sqlglot
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator
from sqlglot import exp

from governed_analytics.evals.models import GeneratedSql, NormalizedFinishReason
from governed_analytics.models.prompts import BASELINE_SYSTEM_PROMPT_V2, build_baseline_user_prompt
from governed_analytics.models.protocols import EvaluationGenerationRequest, SqlGenerationRequest

_SAFE_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ModelAdapterError(ValueError):
    """Stable public adapter error with only safe call-accounting metadata."""

    def __init__(
        self,
        category: str,
        *,
        provider_model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        finish_reason: NormalizedFinishReason | None = None,
        output_truncated: bool = False,
    ) -> None:
        super().__init__(category)
        self.provider_model = provider_model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.finish_reason = finish_reason
        self.output_truncated = output_truncated


class _ErrorMetadata(TypedDict):
    provider_model: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    finish_reason: NormalizedFinishReason | None
    output_truncated: bool


class _ProviderSqlResponse(BaseModel):
    """Strict, intentionally private provider JSON envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sql: StrictStr
    assumptions: list[StrictStr]

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, sql: str) -> str:
        if not _is_single_postgres_query(sql):
            raise ValueError("invalid SQL")
        return sql

    @field_validator("assumptions")
    @classmethod
    def validate_assumptions(cls, assumptions: list[str]) -> list[str]:
        if len(assumptions) > 8 or any(not item.strip() or len(item) > 200 for item in assumptions):
            raise ValueError("invalid assumptions")
        return assumptions


def _error(
    category: str,
    *,
    provider_model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    finish_reason: NormalizedFinishReason | None = None,
    output_truncated: bool = False,
) -> ModelAdapterError:
    return ModelAdapterError(
        category,
        provider_model=provider_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
        output_truncated=output_truncated,
    )


def _validation_category(error: ValidationError) -> str:
    """Map strict envelope failures to stable categories without retaining raw values."""
    locations = tuple(
        item.get("loc", ())
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    )
    if any(location and location[0] == "assumptions" for location in locations):
        return "invalid_assumptions"
    if any(location and location[0] == "sql" for location in locations):
        return "invalid_sql_content"
    return "invalid_envelope"


def _is_single_postgres_query(sql: str) -> bool:
    """Keep malformed provider SQL at the adapter boundary, before the SQL guard."""
    if re.match(r"(?i)^(?:select|with)\b", sql) is None:
        return False
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        return False
    return len(statements) == 1 and isinstance(statements[0], exp.Query)


def _strict_positive_integer(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


def _safe_model_identifier(value: object) -> str | None:
    if (
        isinstance(value, str)
        and _SAFE_MODEL_IDENTIFIER.fullmatch(value) is not None
        and not value.lower().startswith(("sk-", "pk-", "bearer-"))
    ):
        return value
    return None


def _normalise_finish_reason(value: object) -> NormalizedFinishReason | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "unknown"
    if value in {"stop", "length", "content_filter", "tool_calls"}:
        return value  # type: ignore[return-value]
    if value == "function_call":
        return "other"
    return "unknown"


class OpenAICompatibleSqlGenerator:
    """Generate one structured SQL answer through an already-created SDK client."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        safe_model = _safe_model_identifier(model)
        if safe_model is None:
            raise _error("invalid_request")
        self._client = client
        self._model = safe_model

    @property
    def model(self) -> str:
        """Return the configured request alias without exposing client configuration."""
        return self._model

    async def generate(
        self, request: SqlGenerationRequest | EvaluationGenerationRequest
    ) -> GeneratedSql:
        started_at = monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": BASELINE_SYSTEM_PROMPT_V2},
                    {"role": "user", "content": build_baseline_user_prompt(request)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=1200,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception:
            latency_ms = max(0, round((monotonic() - started_at) * 1000))
            raise _error("provider_call_failed", latency_ms=latency_ms) from None
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
            raise _error("missing_content", **metadata) from None
        raw_finish_reason: object = None
        with suppress(Exception):
            raw_finish_reason = choice.finish_reason
        finish_reason = _normalise_finish_reason(raw_finish_reason)
        metadata["finish_reason"] = finish_reason
        metadata["output_truncated"] = finish_reason == "length"
        try:
            content = choice.message.content
        except Exception:
            raise _error("missing_content", **metadata) from None
        if content is None:
            raise _error("missing_content", **metadata)
        if not isinstance(content, str):
            raise _error("invalid_content_type", **metadata)
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise _error("invalid_json", **metadata) from None
        try:
            parsed_content = _ProviderSqlResponse.model_validate(decoded)
        except ValidationError as error:
            raise _error(_validation_category(error), **metadata) from None

        if not usage_accessible or usage is None or not usage_fields_accessible:
            raise _error("missing_usage", **metadata)
        if input_tokens is None or output_tokens is None:
            raise _error("invalid_usage", **metadata)
        if provider_model is None:
            raise _error("missing_model", **metadata)
        return GeneratedSql(
            sql=parsed_content.sql,
            assumptions=tuple(parsed_content.assumptions),
            provider_model=provider_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            output_truncated=finish_reason == "length",
        )
