"""Strict, versioned pricing metadata and exact Decimal cost accounting."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class PricingContractError(ValueError):
    """Stable, sanitized public error for invalid pricing metadata."""


class _DuplicatePricingKeyError(ValueError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _construct_mapping_without_duplicates(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise _DuplicatePricingKeyError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


class ModelPricing(BaseModel):
    """A single-region, single-input-bracket price record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: str
    region: str
    requested_model: str
    resolved_model: str
    effective_date: date
    currency: Literal["CNY"]
    unit_tokens: int
    input_token_upper_bound: int
    input_price: Decimal
    output_price: Decimal
    pricing_basis: str
    source: str
    fx_source: str | None = None

    @field_validator(
        "provider", "region", "requested_model", "resolved_model", "pricing_basis", "source"
    )
    @classmethod
    def validate_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("unit_tokens", "input_token_upper_bound")
    @classmethod
    def validate_strict_positive_integer(cls, value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("input_price", "output_price", mode="before")
    @classmethod
    def validate_decimal_price(cls, value: object) -> Decimal:
        if not isinstance(value, str):
            raise ValueError("price must be a decimal string")
        try:
            price = Decimal(value)
        except (InvalidOperation, ValueError):
            raise ValueError("price must be a finite nonnegative decimal") from None
        if not price.is_finite() or price < 0:
            raise ValueError("price must be a finite nonnegative decimal")
        return price

    @field_validator("source", "fx_source")
    @classmethod
    def validate_source_url(cls, source: str | None) -> str | None:
        if source is None:
            return None
        try:
            parsed_url = urlsplit(source)
            _ = parsed_url.port
        except ValueError as error:
            raise ValueError("source must be a valid HTTPS URL") from error
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or "?" in source
            or "#" in source
        ):
            raise ValueError("source must be a credential-free HTTPS URL")
        return source


def provider_model_has_pricing(provider_model: str, pricing: ModelPricing) -> bool:
    """Return whether an observed provider identity is bound by this price record.

    OpenAI-compatible providers can return either the requested API alias or the
    provider's pinned version label.  The pricing record is the trusted, exact
    mapping between those two identities; no other alias is accepted.
    """

    return provider_model in (pricing.requested_model, pricing.resolved_model)


def _resolve_from_repository(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def load_model_pricing(path: str | Path) -> ModelPricing:
    """Load strict UTF-8 YAML pricing metadata from an explicit path."""
    resolved_path = _resolve_from_repository(path)
    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PricingContractError("pricing metadata unreadable") from None
    try:
        document = yaml.load(raw_text, Loader=_UniqueKeySafeLoader)
    except _DuplicatePricingKeyError:
        raise PricingContractError("pricing metadata duplicate keys") from None
    except (TypeError, yaml.YAMLError):
        raise PricingContractError("pricing metadata malformed") from None
    if not isinstance(document, Mapping):
        raise PricingContractError("pricing metadata invalid")
    try:
        return ModelPricing.model_validate(document)
    except ValidationError:
        raise PricingContractError("pricing metadata invalid") from None


def _validate_token_count(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def estimate_cost_cny(input_tokens: int, output_tokens: int, pricing: ModelPricing) -> Decimal:
    """Calculate an exact request cost within the record's declared input bracket."""
    validated_input_tokens = _validate_token_count(input_tokens, name="input_tokens")
    validated_output_tokens = _validate_token_count(output_tokens, name="output_tokens")
    if validated_input_tokens > pricing.input_token_upper_bound:
        raise ValueError("input token count exceeds recorded price bracket")
    return (
        Decimal(validated_input_tokens) * pricing.input_price / Decimal(pricing.unit_tokens)
        + Decimal(validated_output_tokens) * pricing.output_price / Decimal(pricing.unit_tokens)
    )


__all__ = [
    "ModelPricing",
    "PricingContractError",
    "estimate_cost_cny",
    "load_model_pricing",
    "provider_model_has_pricing",
]
