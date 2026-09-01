from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from governed_analytics.data_generation.models import (
    DatasetScale,
    GeneratorConfig,
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
