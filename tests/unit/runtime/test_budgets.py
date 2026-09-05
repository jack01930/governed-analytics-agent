from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from governed_analytics.agent.contracts import (
    ActionType,
    ModelUsage,
    StopReason,
    StructuredModelRequest,
)
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.pricing import ModelPricing, load_model_pricing
from governed_analytics.runtime.budgets import BudgetExceeded, BudgetLedger, BudgetLimits


def _pricing(
    *,
    input_price: str = "0.10",
    output_price: str = "0.20",
) -> ModelPricing:
    return ModelPricing.model_validate(
        {
            "provider": "fixture",
            "region": "local",
            "requested_model": "fixture-agent",
            "resolved_model": "fixture-agent",
            "effective_date": date(2026, 9, 1),
            "currency": "CNY",
            "unit_tokens": 1000,
            "input_token_upper_bound": 10000,
            "input_price": input_price,
            "output_price": output_price,
            "pricing_basis": "test",
            "source": "https://example.test/pricing",
        }
    )


def _settings(
    *,
    soft_cost_cny: Decimal = Decimal("0.20"),
    hard_cost_cny: Decimal = Decimal("0.30"),
) -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        _env_file=None,  # type: ignore[call-arg]
        soft_cost_cny=soft_cost_cny,
        hard_cost_cny=hard_cost_cny,
    )


def _request(*, max_output_tokens: int = 100) -> StructuredModelRequest:
    return StructuredModelRequest(
        purpose="behavior",
        system_prompt="规则",
        user_payload={"query": "六月GMV"},
        output_schema_name="BehaviorDecision",
        output_schema_summary={"title": "BehaviorDecision", "type": "object"},
        max_output_tokens=max_output_tokens,
    )


def _ledger(
    *,
    settings: AgentRuntimeSettings | None = None,
    pricing: ModelPricing | None = None,
    monotonic: Callable[[], float] | None = None,
) -> BudgetLedger:
    return BudgetLedger(
        limits=BudgetLimits.from_settings(settings or _settings()),
        pricing=pricing or _pricing(input_price="0.00", output_price="0.00"),
        monotonic=monotonic or (lambda: 0.0),
    )


def test_budget_limits_copy_all_fixed_runtime_settings() -> None:
    assert BudgetLimits.from_settings(_settings()) == BudgetLimits(
        max_action_loops=4,
        max_llm_calls=8,
        max_tool_calls=12,
        max_execute_calls=5,
        max_profile_calls=2,
        max_repairs=1,
        timeout_seconds=60,
        soft_cost_cny=Decimal("0.20"),
        hard_cost_cny=Decimal("0.30"),
    )


def test_budget_reserves_prompt_byte_upper_bound_before_model_call() -> None:
    ledger = _ledger(pricing=_pricing())
    request = _request()

    reservation = ledger.reserve_model_call(request)
    settled = ledger.settle_model_call(
        reservation,
        ModelUsage(input_tokens=10, output_tokens=20),
        "fixture-agent",
    )

    assert reservation.input_token_upper_bound == len(request.prompt_bytes())
    assert settled.llm_calls == 1
    assert settled.input_tokens == 10
    assert settled.output_tokens == 20
    assert settled.reserved_cost_cny == Decimal("0")
    assert settled.committed_cost_cny == Decimal("0.005")


def test_budget_rejects_projected_hard_cap_without_incrementing_calls() -> None:
    settings = _settings(
        soft_cost_cny=Decimal("0.01"),
        hard_cost_cny=Decimal("0.02"),
    )
    ledger = _ledger(settings=settings, pricing=_pricing())
    request = StructuredModelRequest(
        purpose="action",
        system_prompt="x" * 200,
        user_payload={"query": "y" * 200},
        output_schema_name="AnalysisAction",
        output_schema_summary={"title": "AnalysisAction", "type": "object"},
        max_output_tokens=100,
    )

    with pytest.raises(BudgetExceeded) as raised:
        ledger.reserve_model_call(request)

    assert raised.value.reason is StopReason.COST_HARD_CAP
    assert ledger.snapshot.llm_calls == 0
    assert ledger.snapshot.reserved_cost_cny == Decimal("0")


def test_eighth_model_call_is_allowed_and_ninth_is_rejected_without_increment() -> None:
    ledger = _ledger()

    for _ in range(8):
        reservation = ledger.reserve_model_call(_request(max_output_tokens=1))
        ledger.settle_model_call(
            reservation,
            ModelUsage(input_tokens=0, output_tokens=0),
            "fixture-agent",
        )

    with pytest.raises(BudgetExceeded) as raised:
        ledger.reserve_model_call(_request(max_output_tokens=1))

    assert raised.value.reason is StopReason.LLM_CALL_LIMIT
    assert ledger.snapshot.llm_calls == 8


def test_failed_model_call_commits_the_full_reservation_once() -> None:
    ledger = _ledger(pricing=_pricing())
    reservation = ledger.reserve_model_call(_request())

    snapshot = ledger.fail_model_call(reservation)

    assert snapshot.committed_cost_cny == reservation.reserved_cost_cny
    assert snapshot.reserved_cost_cny == Decimal("0")
    with pytest.raises(RuntimeError, match="reservation"):
        ledger.fail_model_call(reservation)


def test_provider_model_mismatch_fails_closed_and_retains_reservation() -> None:
    ledger = _ledger(pricing=_pricing())
    reservation = ledger.reserve_model_call(_request())

    with pytest.raises(RuntimeError, match="provider model"):
        ledger.settle_model_call(
            reservation,
            ModelUsage(input_tokens=0, output_tokens=0),
            "different-model",
        )

    assert ledger.snapshot.committed_cost_cny == Decimal("0")
    assert ledger.snapshot.reserved_cost_cny == reservation.reserved_cost_cny
    assert ledger.fail_model_call(reservation).committed_cost_cny == reservation.reserved_cost_cny


def test_deepseek_observed_alias_settles_but_unknown_model_fails_closed() -> None:
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    ledger = _ledger(pricing=pricing)
    accepted = ledger.reserve_model_call(_request(max_output_tokens=1))

    settled = ledger.settle_model_call(
        accepted,
        ModelUsage(input_tokens=1, output_tokens=1),
        "deepseek-v4-flash",
    )

    assert settled.input_tokens == 1
    assert settled.output_tokens == 1
    rejected = ledger.reserve_model_call(_request(max_output_tokens=1))
    with pytest.raises(RuntimeError, match="provider model"):
        ledger.settle_model_call(
            rejected,
            ModelUsage(input_tokens=1, output_tokens=1),
            "deepseek-v4-flash-unknown",
        )
    assert ledger.snapshot.reserved_cost_cny == rejected.reserved_cost_cny


def test_usage_cost_above_reservation_fails_closed() -> None:
    ledger = _ledger(pricing=_pricing())
    reservation = ledger.reserve_model_call(_request(max_output_tokens=1))

    with pytest.raises(RuntimeError, match="reserved cost"):
        ledger.settle_model_call(
            reservation,
            ModelUsage(input_tokens=reservation.input_token_upper_bound, output_tokens=2),
            "fixture-agent",
        )

    assert ledger.snapshot.reserved_cost_cny == reservation.reserved_cost_cny
    assert ledger.snapshot.committed_cost_cny == Decimal("0")


def test_successful_model_call_can_only_be_settled_once() -> None:
    ledger = _ledger(pricing=_pricing())
    reservation = ledger.reserve_model_call(_request())
    ledger.settle_model_call(
        reservation,
        ModelUsage(input_tokens=0, output_tokens=0),
        "fixture-agent",
    )

    with pytest.raises(RuntimeError, match="reservation"):
        ledger.settle_model_call(
            reservation,
            ModelUsage(input_tokens=0, output_tokens=0),
            "fixture-agent",
        )


def test_committed_cost_at_soft_cap_sets_the_snapshot_flag() -> None:
    ledger = _ledger(
        settings=_settings(
            soft_cost_cny=Decimal("0.005"),
            hard_cost_cny=Decimal("1.00"),
        ),
        pricing=_pricing(),
    )
    reservation = ledger.reserve_model_call(_request())

    snapshot = ledger.settle_model_call(
        reservation,
        ModelUsage(input_tokens=10, output_tokens=20),
        "fixture-agent",
    )

    assert snapshot.committed_cost_cny == Decimal("0.005")
    assert snapshot.soft_cap_reached is True


@pytest.mark.parametrize(
    ("action_type", "allowed", "reason"),
    [
        (ActionType.EXECUTE_SQL, 5, StopReason.EXECUTE_LIMIT),
        (ActionType.PROFILE, 2, StopReason.PROFILE_LIMIT),
        (ActionType.METRIC_LOOKUP, 12, StopReason.TOOL_CALL_LIMIT),
    ],
)
def test_tool_budgets_allow_the_limit_and_atomically_reject_the_next_call(
    action_type: ActionType,
    allowed: int,
    reason: StopReason,
) -> None:
    ledger = _ledger()

    for _ in range(allowed):
        ledger.consume_tool(action_type)
    before = ledger.snapshot

    with pytest.raises(BudgetExceeded) as raised:
        ledger.consume_tool(action_type)

    assert raised.value.reason is reason
    assert ledger.snapshot == before


def test_four_action_loops_are_allowed_and_fifth_is_rejected() -> None:
    ledger = _ledger()

    for _ in range(4):
        ledger.ensure_action_loop_available()
        ledger.consume_action_loop()
    before = ledger.snapshot

    with pytest.raises(BudgetExceeded) as raised:
        ledger.ensure_action_loop_available()

    assert raised.value.reason is StopReason.ANALYSIS_LOOP_LIMIT
    assert ledger.snapshot == before


def test_one_repair_is_allowed_and_second_is_rejected_without_increment() -> None:
    ledger = _ledger()
    ledger.consume_repair()
    before = ledger.snapshot

    with pytest.raises(BudgetExceeded) as raised:
        ledger.consume_repair()

    assert raised.value.reason is StopReason.REPAIR_FAILED
    assert ledger.snapshot == before


def test_elapsed_time_at_deadline_is_rejected() -> None:
    now = 100.0
    ledger = _ledger(monotonic=lambda: now)
    assert ledger.snapshot.deadline_monotonic == 160.0

    now = 159.999
    ledger.ensure_time_remaining()
    now = 160.0

    with pytest.raises(BudgetExceeded) as raised:
        ledger.ensure_time_remaining()

    assert raised.value.reason is StopReason.TASK_TIMEOUT


def test_zero_price_fixture_still_counts_model_calls() -> None:
    ledger = _ledger()
    reservation = ledger.reserve_model_call(_request())

    snapshot = ledger.fail_model_call(reservation)

    assert snapshot.llm_calls == 1
    assert snapshot.committed_cost_cny == Decimal("0.00")
