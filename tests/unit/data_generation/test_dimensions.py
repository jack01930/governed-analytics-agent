"""Contracts for canonical, deterministic business dimensions."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation.dimensions import (
    generate_campaigns,
    generate_categories,
    generate_customers,
    generate_products,
)
from governed_analytics.data_generation.models import load_generator_config
from governed_analytics.data_generation.vocabulary import (
    CATEGORY_NAMES,
    CHANNELS,
    CUSTOMER_SEGMENTS,
    REGIONS,
)


def test_tiny_dimensions_have_stable_keys_counts_and_schema_order() -> None:
    """Every dimension uses the database schema's column order and stable business keys."""
    config = load_generator_config("data/generator/tiny.yaml")

    categories = generate_categories()
    customers = generate_customers(config)
    products = generate_products(config)
    campaigns = generate_campaigns(config)

    assert list(categories.columns) == [
        "category_id",
        "category_code",
        "category_name",
        "created_at",
    ]
    assert list(customers.columns) == [
        "customer_id",
        "customer_code",
        "segment",
        "region",
        "registered_at",
    ]
    assert list(products.columns) == [
        "product_id",
        "sku",
        "category_id",
        "product_name",
        "list_price",
        "unit_cost",
        "is_active",
    ]
    assert list(campaigns.columns) == [
        "campaign_id",
        "campaign_code",
        "campaign_name",
        "channel",
        "start_at",
        "end_at",
        "spend",
    ]
    assert categories["category_id"].tolist() == list(range(1, 21))
    assert categories["category_code"].tolist() == [f"CAT-{index:03d}" for index in range(1, 21)]
    assert categories["category_name"].tolist() == list(CATEGORY_NAMES)
    assert len(customers) == 500
    assert customers.iloc[0]["customer_code"] == "CUS-000001"
    assert customers.iloc[-1]["customer_code"] == "CUS-000500"
    assert len(products) == 100
    assert products.iloc[0]["sku"] == "SKU-000001"
    assert products.iloc[-1]["sku"] == "SKU-000100"
    assert len(campaigns) == 6
    assert campaigns.iloc[0]["campaign_code"] == "CAM-000001"
    assert campaigns.iloc[-1]["campaign_code"] == "CAM-000006"


def test_dimensions_are_repeatable_and_do_not_mutate_config() -> None:
    """Named streams yield identical frames without changing their immutable input."""
    config = load_generator_config("data/generator/tiny.yaml")
    original = config.model_dump()

    for generator in (generate_customers, generate_products, generate_campaigns):
        first = generator(config)
        second = generator(config)
        assert first.equals(second)

    assert config.model_dump() == original


def test_customer_vocabulary_and_registration_window_are_valid() -> None:
    """Customers are registered before all business-period orders could occur."""
    config = load_generator_config("data/generator/tiny.yaml")
    customers = generate_customers(config)

    assert set(customers["segment"]) <= set(CUSTOMER_SEGMENTS)
    assert set(customers["region"]) <= set(REGIONS)
    assert customers["registered_at"].dt.tz is not None
    assert customers["registered_at"].min() >= config.start_at - timedelta(days=730)
    assert customers["registered_at"].max() < config.start_at
    assert customers["registered_at"].max() < config.end_at


def test_products_keep_exact_money_values_and_business_ranges() -> None:
    """Prices and costs are Decimal cents, not floats or prematurely formatted strings."""
    config = load_generator_config("data/generator/tiny.yaml")
    products = generate_products(config)

    assert set(products["category_id"]) <= set(range(1, 21))
    assert products["list_price"].map(type).eq(Decimal).all()
    assert products["unit_cost"].map(type).eq(Decimal).all()
    assert products["list_price"].map(lambda value: value.as_tuple().exponent == -2).all()
    assert products["unit_cost"].map(lambda value: value.as_tuple().exponent == -2).all()
    assert products["list_price"].between(Decimal("19.00"), Decimal("4999.00")).all()
    assert (products["unit_cost"] <= products["list_price"]).all()
    assert (products["unit_cost"] >= Decimal("0.00")).all()


def test_campaigns_are_non_organic_non_overlapping_and_in_business_window() -> None:
    """Campaign timing and spend satisfy the fixed marketing dimension contract."""
    config = load_generator_config("data/generator/tiny.yaml")
    campaigns = generate_campaigns(config)

    assert set(campaigns["channel"]) <= (set(CHANNELS) - {"organic"})
    assert campaigns["spend"].map(type).eq(Decimal).all()
    assert campaigns["spend"].map(lambda value: value.as_tuple().exponent == -2).all()
    assert campaigns["spend"].between(Decimal("5000.00"), Decimal("100000.00")).all()
    assert (campaigns["end_at"] - campaigns["start_at"] == pd.Timedelta(days=14)).all()
    assert campaigns["start_at"].min() >= config.start_at
    assert campaigns["end_at"].max() <= config.end_at
    following_starts = campaigns["start_at"].iloc[1:].reset_index(drop=True)
    preceding_ends = campaigns["end_at"].iloc[:-1].reset_index(drop=True)
    assert (following_starts >= preceding_ends).all()


def test_load_generator_config_rejects_empty_or_non_mapping_yaml(tmp_path: Path) -> None:
    """Configuration loading only accepts a non-empty YAML mapping."""
    empty_path = tmp_path / "empty.yaml"
    empty_path.write_text("", encoding="utf-8")
    list_path = tmp_path / "list.yaml"
    list_path.write_text("- tiny\n", encoding="utf-8")

    for path in (empty_path, list_path):
        with pytest.raises(ValueError, match="non-empty YAML mapping"):
            load_generator_config(path)
