"""Deterministic, zero-cost structured Agent-model sessions for tests and demos."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ValidationError

from governed_analytics.agent.contracts import (
    AgentModelErrorCategory,
    ModelPurpose,
    ModelUsage,
    StructuredModelRequest,
    StructuredModelResult,
)
from governed_analytics.agent.ports import AgentModelError

type ScriptEntry = Mapping[str, object]
type PurposeScripts = Mapping[ModelPurpose, Sequence[ScriptEntry]]
type AgentScripts = Mapping[str, PurposeScripts]


def _normalise_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).casefold()


class ScriptedAgentModel:
    """A mutable one-run cursor over an immutable structured-output library."""

    def __init__(self, scripts: AgentScripts) -> None:
        self._scripts = scripts
        self._ordinals: dict[tuple[str, ModelPurpose], int] = {}
        self._current_query: str | None = None

    @property
    def model(self) -> str:
        return "fixture-agent"

    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredModelResult[T]:
        if request.output_schema_name != output_type.__name__:
            raise AgentModelError(AgentModelErrorCategory.SCHEMA_IDENTITY_MISMATCH)

        if request.purpose == "repair":
            query = self._current_query
        else:
            raw_query = request.user_payload.get("query")
            query = _normalise_query(raw_query) if isinstance(raw_query, str) else None
            self._current_query = query
        if query is None:
            raise AgentModelError(
                AgentModelErrorCategory.FIXTURE_SCRIPT_MISMATCH,
                provider_model=self.model,
            )
        key = (query, request.purpose)
        ordinal = self._ordinals.get(key, 0)
        try:
            script = self._scripts[query][request.purpose][ordinal]
        except (KeyError, IndexError, TypeError):
            raise AgentModelError(
                AgentModelErrorCategory.FIXTURE_SCRIPT_MISMATCH,
                provider_model=self.model,
            ) from None
        self._ordinals[key] = ordinal + 1
        try:
            output = output_type.model_validate(script)
        except ValidationError:
            raise AgentModelError(
                AgentModelErrorCategory.INVALID_STRUCTURE,
                provider_model=self.model,
            ) from None
        return StructuredModelResult(
            output=output,
            provider_model=self.model,
            usage=ModelUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


__all__ = ["AgentScripts", "PurposeScripts", "ScriptedAgentModel"]
