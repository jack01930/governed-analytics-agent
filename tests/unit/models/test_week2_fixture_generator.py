from __future__ import annotations

from collections import OrderedDict

import pytest

from governed_analytics.models.fixtures import (
    FixtureContractError,
    Week2FixtureSqlGenerator,
)
from governed_analytics.models.protocols import EvaluationGenerationRequest


def _ids() -> tuple[str, ...]:
    return tuple(
        [f"C2{number:02d}" for number in range(1, 21)]
        + [f"P2{number:02d}" for number in range(1, 21)]
        + [f"B2{number:02d}" for number in range(1, 11)]
    )


def _request(case_id: str = "C201") -> EvaluationGenerationRequest:
    return EvaluationGenerationRequest(
        case_id=case_id,
        question="2026 年 6 月 GMV 是多少?",
        schema_context="TABLE orders",
        metric_context="METRIC gmv",
    )


@pytest.mark.asyncio
async def test_week2_fixture_is_complete_ordered_and_zero_cost() -> None:
    fixture = Week2FixtureSqlGenerator(
        OrderedDict((case_id, "select 1 as value") for case_id in _ids())
    )

    result = await fixture.generate(_request())

    assert fixture.case_ids == _ids()
    assert result.sql == "select 1 as value"
    assert result.provider_model == "fixture-oracle"
    assert result.input_tokens == result.output_tokens == result.latency_ms == 0
    assert result.finish_reason is None
    assert result.output_truncated is False


@pytest.mark.parametrize(
    "mapping",
    (
        {},
        {case_id: "select 1 as value" for case_id in _ids()[:-1]},
        {case_id: "select 1; select 2" for case_id in _ids()},
    ),
)
def test_week2_fixture_rejects_incomplete_or_invalid_contracts(
    mapping: dict[str, str],
) -> None:
    with pytest.raises(FixtureContractError, match="Week 2 fixture"):
        Week2FixtureSqlGenerator(mapping)


def test_week2_request_does_not_widen_legacy_case_id_contract() -> None:
    with pytest.raises(ValueError):
        _request("G001")
    with pytest.raises(ValueError):
        _request("S201")
