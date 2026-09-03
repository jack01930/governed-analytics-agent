from datetime import timedelta
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from governed_analytics.data_generation.models import (
    GENERATOR_CONTRACT_VERSION,
    DatasetManifest,
    DatasetScale,
    GeneratorConfig,
    TableDigest,
    dataset_id_for_config,
    generator_config_sha256,
)


def load_config(name: str) -> GeneratorConfig:
    raw = yaml.safe_load(Path(f"data/generator/{name}.yaml").read_text(encoding="utf-8"))
    return GeneratorConfig.model_validate(raw)


def test_tiny_config_is_frozen_and_exact() -> None:
    config = load_config("tiny")

    assert config.scale is DatasetScale.TINY
    assert config.seed == 20260901
    assert config.customers == 500
    assert config.products == 100
    assert config.orders == 3000
    assert config.campaigns == 6
    assert config.start_at.tzinfo is not None
    assert config.start_at.utcoffset() == timedelta(0)
    assert config.end_at.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        config.orders = 1


def test_full_config_has_fixed_counts_and_interval() -> None:
    config = load_config("full")

    assert config.scale is DatasetScale.FULL
    assert config.seed == 20260901
    assert config.customers == 50000
    assert config.products == 2000
    assert config.orders == 300000
    assert config.campaigns == 30
    assert config.end_at > config.start_at
    assert config.campaigns * timedelta(days=14) <= config.end_at - config.start_at


@pytest.mark.parametrize(
    ("start_at", "end_at"),
    [
        ("2025-01-01T00:00:00", "2026-07-01T00:00:00Z"),
        ("2025-01-01T00:00:00Z", "2026-07-01T00:00:00"),
        ("2026-07-01T00:00:00Z", "2025-01-01T00:00:00Z"),
    ],
)
def test_config_rejects_naive_or_non_increasing_intervals(start_at: str, end_at: str) -> None:
    raw = yaml.safe_load(Path("data/generator/tiny.yaml").read_text(encoding="utf-8"))
    raw["start_at"] = start_at
    raw["end_at"] = end_at

    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_config_rejects_non_utc_offsets() -> None:
    raw = yaml.safe_load(Path("data/generator/tiny.yaml").read_text(encoding="utf-8"))
    raw["start_at"] = "2025-01-01T00:00:00+08:00"
    raw["end_at"] = "2026-07-01T00:00:00+08:00"

    with pytest.raises(ValidationError, match="UTC offset"):
        GeneratorConfig.model_validate(raw)


def test_config_rejects_campaigns_beyond_14_day_non_overlapping_capacity() -> None:
    raw = yaml.safe_load(Path("data/generator/tiny.yaml").read_text(encoding="utf-8"))
    raw["campaigns"] = 40

    with pytest.raises(ValidationError, match="14-day non-overlapping campaign capacity"):
        GeneratorConfig.model_validate(raw)


@pytest.mark.parametrize("campaign_count", (0, 1, 4))
def test_config_rejects_campaign_counts_without_q2_scoreable_capacity(campaign_count: int) -> None:
    """Every accepted profile must be able to supply five Q2 campaign score rows."""
    raw = yaml.safe_load(Path("data/generator/tiny.yaml").read_text(encoding="utf-8"))
    raw["campaigns"] = campaign_count

    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_config_accepts_the_minimum_five_campaigns_with_existing_interval_contract() -> None:
    """The new consumer boundary composes with UTC and 14-day capacity validation."""
    raw = yaml.safe_load(Path("data/generator/tiny.yaml").read_text(encoding="utf-8"))
    raw["campaigns"] = 5

    config = GeneratorConfig.model_validate(raw)

    assert config.campaigns == 5
    assert config.start_at.utcoffset() == timedelta(0)
    assert config.end_at.utcoffset() == timedelta(0)
    assert config.campaigns * timedelta(days=14) <= config.end_at - config.start_at


def test_generator_config_hash_and_dataset_id_are_stable_and_sensitive() -> None:
    config = load_config("tiny")
    changed_config = config.model_copy(update={"orders": config.orders + 1})

    config_hash = generator_config_sha256(config)
    dataset_id = dataset_id_for_config(config)

    assert config_hash == generator_config_sha256(config)
    assert config_hash != generator_config_sha256(changed_config)
    assert dataset_id == dataset_id_for_config(config)
    assert dataset_id != dataset_id_for_config(changed_config)
    assert len(config_hash) == 64
    assert len(dataset_id) == 64
    assert all(character in "0123456789abcdef" for character in config_hash)
    assert all(character in "0123456789abcdef" for character in dataset_id)


def test_consumer_signal_producer_revision_has_an_explicit_contract_version() -> None:
    """A changed fixed-seed truth cannot silently retain the prior dataset identity contract."""
    assert GENERATOR_CONTRACT_VERSION == "1.1.0"


def test_dataset_manifest_rejects_unknown_fields_bad_hashes_and_table_order() -> None:
    table = TableDigest(table_name="categories", row_count=0, sha256="0" * 64)
    payload = {
        "dataset_id": "1" * 64,
        "config_sha256": "2" * 64,
        "seed": 20260901,
        "scale": "tiny",
        "tables": [table.model_dump()],
        "anomaly_manifest_path": "anomaly_manifest.json",
        "unexpected": True,
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DatasetManifest.model_validate(payload)
    with pytest.raises(ValidationError, match="String should match pattern"):
        TableDigest(table_name="categories", row_count=0, sha256="BAD")
    with pytest.raises(ValidationError, match="table names/order"):
        DatasetManifest.model_validate(
            {key: value for key, value in payload.items() if key != "unexpected"}
        )
