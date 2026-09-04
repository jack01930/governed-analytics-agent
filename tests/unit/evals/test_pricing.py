from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from governed_analytics.evals.pricing import ModelPricing as EvalModelPricing
from governed_analytics.evals.pricing import (
    PricingContractError,
    estimate_cost_cny,
    load_model_pricing,
)
from governed_analytics.pricing import ModelPricing
from governed_analytics.pricing import load_model_pricing as load_public_model_pricing

PRICING_PATH = "data/pricing/deepseek-v4-flash-2026-09-01.yaml"


def test_evals_pricing_remains_a_compatible_public_facade() -> None:
    assert EvalModelPricing is ModelPricing


def test_pricing_record_is_versioned_and_cost_is_exact_decimal() -> None:
    pricing = load_model_pricing(PRICING_PATH)

    assert pricing.provider == "deepseek"
    assert pricing.requested_model == "deepseek-v4-flash"
    assert pricing.resolved_model == "DeepSeek-V4-Flash-0731"
    assert pricing.pricing_basis == "peak cache-miss upper bound converted at USD/CNY 6.7809"
    assert pricing.fx_source == "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
    assert pricing.input_token_upper_bound == 1_000_000
    assert estimate_cost_cny(200_000, 1_000_000, pricing) == Decimal("9.5475072")
    assert estimate_cost_cny(0, 0, pricing) == Decimal("0.00")
    assert estimate_cost_cny(1_000_000, 1_000_000, pricing) == Decimal("11.934384")


@pytest.mark.parametrize("value", (-1, True, 1.0, "1"))
def test_cost_rejects_non_strict_or_negative_token_counts(value: object) -> None:
    pricing = load_model_pricing(PRICING_PATH)

    with pytest.raises(ValueError):
        estimate_cost_cny(value, 0, pricing)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        estimate_cost_cny(0, value, pricing)  # type: ignore[arg-type]


def test_cost_fails_closed_when_input_exceeds_recorded_bracket() -> None:
    pricing = load_model_pricing(PRICING_PATH)

    with pytest.raises(ValueError, match="price bracket"):
        estimate_cost_cny(1_000_001, 1, pricing)


@pytest.mark.parametrize(
    ("field", "original", "replacement"),
    [
        ("input_price", '"2.983596"', '"not-a-decimal"'),
        ("input_price", '"2.983596"', '"NaN"'),
        ("output_price", '"8.950788"', '"Infinity"'),
        ("output_price", '"8.950788"', '"-0.01"'),
    ],
)
def test_pricing_loader_rejects_invalid_prices(
    tmp_path: Path, field: str, original: str, replacement: str
) -> None:
    source = (Path(__file__).resolve().parents[3] / PRICING_PATH).read_text(encoding="utf-8")
    path = tmp_path / "pricing.yaml"
    content = source.replace(f"{field}: {original}", f"{field}: {replacement}")
    path.write_text(content, encoding="utf-8")

    with pytest.raises(PricingContractError, match="invalid"):
        load_model_pricing(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("provider: a\nprovider: b\n", "duplicate"),
        ("outer:\n  x: 1\n  x: 2\n", "duplicate"),
        ("[not, a, mapping]", "invalid"),
        ("provider: only-one-field\n", "invalid"),
        (
            "provider: deepseek\n"
            "region: global\n"
            "requested_model: deepseek-v4-flash\n"
            "resolved_model: DeepSeek-V4-Flash-0731\n"
            "effective_date: 2026-09-01\n"
            "currency: CNY\n"
            "unit_tokens: 1000000\n"
            "input_token_upper_bound: 1\n"
            "input_price: '2.983596'\n"
            "output_price: '8.950788'\n"
            "pricing_basis: peak cache-miss upper bound\n"
            "source: http://example.com\n"
            "fx_source: https://example.com/fx\n"
            "unexpected: true\n",
            "invalid",
        ),
        ("{", "malformed"),
    ],
)
def test_pricing_loader_rejects_invalid_versioned_metadata(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(PricingContractError, match=message) as error:
        load_model_pricing(path)

    assert "example.com" not in str(error.value)


def test_pricing_loader_is_cwd_independent_and_sanitizes_unreadable_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_model_pricing(PRICING_PATH).currency == "CNY"
    assert load_public_model_pricing(PRICING_PATH).currency == "CNY"

    broken = tmp_path / "bad.yaml"
    broken.write_bytes(b"\xff")
    with pytest.raises(PricingContractError, match="unreadable") as error:
        load_model_pricing(broken)
    assert "utf" not in str(error.value).lower()


def test_pricing_loader_sanitizes_unhashable_yaml_mapping_keys(tmp_path: Path) -> None:
    path = tmp_path / "unhashable-key.yaml"
    path.write_text("? [a, b]\n: value\n", encoding="utf-8")

    with pytest.raises(
        PricingContractError, match=r"^pricing metadata (invalid|malformed)$"
    ) as error:
        load_model_pricing(path)

    assert error.value.__cause__ is None
    assert "unhashable" not in str(error.value).lower()
