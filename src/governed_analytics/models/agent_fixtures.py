"""Deterministic, zero-cost structured Agent-model sessions for tests and demos."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ValidationError

from governed_analytics.agent.contracts import (
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

    @property
    def model(self) -> str:
        return "fixture-agent"

    async def invoke[T: BaseModel](
        self,
        request: StructuredModelRequest,
        output_type: type[T],
    ) -> StructuredModelResult[T]:
        if request.output_schema_name != output_type.__name__:
            raise AgentModelError("schema_identity_mismatch")

        raw_query = request.user_payload.get("query")
        if not isinstance(raw_query, str):
            raise AgentModelError("fixture_script_mismatch", provider_model=self.model)
        query = _normalise_query(raw_query)
        key = (query, request.purpose)
        ordinal = self._ordinals.get(key, 0)
        try:
            script = self._scripts[query][request.purpose][ordinal]
        except (KeyError, IndexError, TypeError):
            raise AgentModelError("fixture_script_mismatch", provider_model=self.model) from None
        self._ordinals[key] = ordinal + 1
        try:
            output = output_type.model_validate(script)
        except ValidationError:
            raise AgentModelError("invalid_structure", provider_model=self.model) from None
        return StructuredModelResult(
            output=output,
            provider_model=self.model,
            usage=ModelUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


__all__ = ["AgentScripts", "PurposeScripts", "ScriptedAgentModel"]
