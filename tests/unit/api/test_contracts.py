from decimal import Decimal

import pytest
from pydantic import ValidationError

from governed_analytics.agent.contracts import RunLifecycleStatus
from governed_analytics.api.contracts import (
    AnalysisCreateRequest,
    AnalysisStatusResponse,
    TraceResponse,
)


@pytest.mark.parametrize("query", ["", " ", "\n\t", "x" * 4001])
def test_create_request_rejects_blank_or_oversized_query(query: str) -> None:
    with pytest.raises(ValidationError):
        AnalysisCreateRequest(query=query)


def test_create_request_forbids_runtime_or_provider_overrides() -> None:
    with pytest.raises(ValidationError):
        AnalysisCreateRequest(
            query="2026年6月GMV是多少？",  # noqa: RUF001
            runtime_mode="live",  # type: ignore[call-arg]
        )


def test_status_and_trace_wire_models_are_strict_and_frozen() -> None:
    status = AnalysisStatusResponse(
        run_id="run-1",
        lifecycle_status=RunLifecycleStatus.QUEUED,
    )
    trace = TraceResponse(
        run_id="run-1",
        snapshot_complete=False,
        nodes=(),
        model_calls=(),
        tool_calls=(),
        evidence_gaps=(),
        repair_count=0,
        model_call_count=0,
        tool_call_count=0,
        input_tokens=0,
        output_tokens=0,
        committed_cost_cny=Decimal("0"),
        stop_reason=None,
    )

    assert status.final_status is None
    assert trace.committed_cost_cny == Decimal("0")
    with pytest.raises(ValidationError):
        AnalysisStatusResponse(
            run_id="run-1",
            lifecycle_status=RunLifecycleStatus.QUEUED,
            sql="select secret",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        TraceResponse.model_validate({**trace.model_dump(), "provider_raw": "secret"})
    assert status.model_config["frozen"] is True


def test_public_contracts_define_no_forbidden_payload_fields() -> None:
    forbidden = {
        "sql",
        "parameters",
        "rows",
        "prompt",
        "endpoint",
        "provider_raw",
        "oracle",
        "expected_rows",
        "expected_sql",
    }
    for model in (AnalysisStatusResponse, TraceResponse):
        assert forbidden.isdisjoint(name.casefold() for name in model.model_fields)
