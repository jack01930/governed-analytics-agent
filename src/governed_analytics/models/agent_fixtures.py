"""Deterministic, zero-cost structured Agent-model sessions for tests and demos."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast

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

    def __init__(self, scripts: AgentScripts, *, unsupported_fallback: bool = False) -> None:
        self._scripts = scripts
        self._unsupported_fallback = unsupported_fallback
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
            if self._unsupported_fallback and request.purpose == "behavior":
                script = {
                    "action": "unsupported",
                    "reason_code": "unsupported_analysis",
                    "missing_fields": (),
                    "user_message": "当前分析场景暂不支持。",
                }
            else:
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


def _freeze_script(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_script(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_script(item) for item in value)
    return value


def _action(contract_id: str, hypothesis_id: str, sql: str) -> dict[str, object]:
    return {
        "action_type": "execute_sql",
        "purpose": contract_id,
        "arguments": {"sql": sql},
        "hypothesis_id": hypothesis_id,
        "contract_id": contract_id,
        "expected_evidence": "contracted numeric result",
    }


def builtin_demo_scripts() -> AgentScripts:
    """Return the shared immutable two-query demo library used by API fixture mode."""

    behavior = {
        "action": "execute",
        "reason_code": "ready",
        "missing_fields": (),
        "user_message": "开始分析。",
    }
    simple_query = _normalise_query("2026年6月GMV是多少？")  # noqa: RUF001
    attribution_query = _normalise_query(
        # The full-width comma is part of the exact approved demo query.
        "比较 2026-06-01 至 06-08 与 06-08 至 06-15 的 "
        "GMV，并按区域、SKU、客户分群解释下降。"  # noqa: RUF001
    )
    simple_plan = {
        "plan_id": "simple-plan",
        "revision": 1,
        "metric_id": "gmv",
        "metric_version": "1.0.0",
        "analysis_type": "simple",
        "windows": (
            {
                "label": "current",
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-07-01T00:00:00Z",
            },
        ),
        "null_policy": "preserve",
        "zero_denominator_policy": "return_null",
        "fill_policy": "none",
        "hypotheses": ({"hypothesis_id": "metric_value", "kind": "metric_value"},),
    }
    attribution_plan = {
        "plan_id": "gmv-attribution-plan",
        "revision": 1,
        "metric_id": "gmv",
        "metric_version": "1.0.0",
        "analysis_type": "attribution",
        "windows": (
            {
                "label": "previous",
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-06-08T00:00:00Z",
            },
            {
                "label": "current",
                "start_at": "2026-06-08T00:00:00Z",
                "end_at": "2026-06-15T00:00:00Z",
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
    }
    raw: dict[str, object] = {
        simple_query: {
            "behavior": (behavior,),
            "plan": (simple_plan,),
            "action": (
                _action(
                    "metric_value_contract",
                    "metric_value",
                    "select cast(125 as numeric) as gmv",
                ),
            ),
            "synthesis": (
                {
                    "status": "completed",
                    "stop_reason": "answer_complete",
                    "answer": "GMV 分析已完成。",
                    "evidence_ids": (),
                },
            ),
        },
        attribution_query: {
            "behavior": (behavior,),
            "plan": (attribution_plan,),
            "action": (
                _action(
                    "gmv_comparison",
                    "confirm_decline",
                    "select 80::numeric as current_gmv, "
                    "100::numeric as previous_gmv, "
                    "-0.2::numeric as change_rate",
                ),
                _action(
                    "region_contribution",
                    "region_contribution",
                    "select 'north' as region, 12::numeric as gmv_loss",
                ),
                _action(
                    "sku_contribution",
                    "sku_contribution",
                    "select 'sku-1' as sku, 9::numeric as gmv_loss",
                ),
                _action(
                    "segment_contribution",
                    "segment_contribution",
                    "select 'vip' as segment, "
                    "100::numeric as previous_gmv, "
                    "80::numeric as current_gmv, -20::numeric as delta",
                ),
            ),
            "synthesis": (
                {
                    "status": "completed",
                    "stop_reason": "answer_complete",
                    "answer": "GMV 下降归因分析已完成。",
                    "evidence_ids": (),
                },
            ),
        },
    }
    return cast(AgentScripts, _freeze_script(raw))


__all__ = [
    "AgentScripts",
    "PurposeScripts",
    "ScriptedAgentModel",
    "builtin_demo_scripts",
]
