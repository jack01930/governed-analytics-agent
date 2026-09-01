"""Deterministic generators for canonical ecommerce dimensions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from governed_analytics.data_generation.models import GeneratorConfig
from governed_analytics.data_generation.randomness import named_rng
from governed_analytics.data_generation.vocabulary import (
    CATEGORY_NAMES,
    CHANNELS,
    CUSTOMER_SEGMENTS,
    REGION_WEIGHTS,
    REGIONS,
)

_CENT = Decimal("0.01")
_CATEGORY_CREATED_AT = datetime(2025, 1, 1, tzinfo=UTC)


def cents_to_decimal(cents: int) -> Decimal:
    """Convert integer cents into an exact two-decimal-place money value."""
    return (Decimal(cents) / Decimal(100)).quantize(_CENT)


def generate_categories() -> pd.DataFrame:
    """Generate the fixed category dimension in PostgreSQL schema column order."""
    category_ids = list(range(1, len(CATEGORY_NAMES) + 1))
    return pd.DataFrame(
        {
            "category_id": category_ids,
            "category_code": [f"CAT-{category_id:03d}" for category_id in category_ids],
            "category_name": list(CATEGORY_NAMES),
            "created_at": [_CATEGORY_CREATED_AT] * len(category_ids),
        }
    )


def generate_customers(config: GeneratorConfig) -> pd.DataFrame:
    """Generate customers registered before the business interval begins."""
    rng = named_rng(config.seed, "customers")
    customer_ids = list(range(1, config.customers + 1))
    registration_offsets = rng.integers(-730 * 24 * 60 * 60, 0, size=config.customers)
    region_probabilities = np.array(REGION_WEIGHTS) / sum(REGION_WEIGHTS)
    regions = rng.choice(REGIONS, size=config.customers, p=region_probabilities)
    segments = rng.choice(CUSTOMER_SEGMENTS, size=config.customers, p=(0.25, 0.60, 0.15))

    return pd.DataFrame(
        {
            "customer_id": customer_ids,
            "customer_code": [f"CUS-{customer_id:06d}" for customer_id in customer_ids],
            "segment": segments.tolist(),
            "region": regions.tolist(),
            "registered_at": [
                config.start_at + timedelta(seconds=int(offset)) for offset in registration_offsets
            ],
        }
    )


def generate_products(config: GeneratorConfig) -> pd.DataFrame:
    """Generate active products with exact Decimal prices and costs from integer cents."""
    rng = named_rng(config.seed, "products")
    product_ids = list(range(1, config.products + 1))
    category_ids = rng.integers(1, len(CATEGORY_NAMES) + 1, size=config.products)
    list_price_cents = np.clip(
        np.rint(rng.lognormal(mean=5.0, sigma=0.8, size=config.products) * 100).astype(int),
        1900,
        499900,
    )
    cost_ratios = rng.uniform(0.35, 0.75, size=config.products)
    unit_cost_cents = [
        min(int(list_price), int(np.floor(int(list_price) * float(cost_ratio))))
        for list_price, cost_ratio in zip(list_price_cents, cost_ratios, strict=True)
    ]

    return pd.DataFrame(
        {
            "product_id": product_ids,
            "sku": [f"SKU-{product_id:06d}" for product_id in product_ids],
            "category_id": category_ids.tolist(),
            "product_name": [
                f"{CATEGORY_NAMES[int(category_id) - 1]}商品{product_id:04d}"
                for product_id, category_id in zip(product_ids, category_ids, strict=True)
            ],
            "list_price": [cents_to_decimal(int(cents)) for cents in list_price_cents],
            "unit_cost": [cents_to_decimal(cents) for cents in unit_cost_cents],
            "is_active": [True] * config.products,
        }
    )


def generate_campaigns(config: GeneratorConfig) -> pd.DataFrame:
    """Generate non-overlapping 14-day non-organic marketing campaigns."""
    rng = named_rng(config.seed, "campaigns")
    campaign_ids = list(range(1, config.campaigns + 1))
    start_times = [
        config.start_at + timedelta(days=14 * index) for index in range(config.campaigns)
    ]
    channels = rng.choice(CHANNELS[1:], size=config.campaigns)
    spend_cents = rng.integers(500000, 10000001, size=config.campaigns)

    return pd.DataFrame(
        {
            "campaign_id": campaign_ids,
            "campaign_code": [f"CAM-{campaign_id:06d}" for campaign_id in campaign_ids],
            "campaign_name": [f"营销活动{campaign_id:03d}" for campaign_id in campaign_ids],
            "channel": channels.tolist(),
            "start_at": start_times,
            "end_at": [start_at + timedelta(days=14) for start_at in start_times],
            "spend": [cents_to_decimal(int(cents)) for cents in spend_cents],
        }
    )
