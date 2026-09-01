"""Immutable public contracts for deterministic data generation.

Money values are generated from integer cents and remain exact ``Decimal`` values.
Only the CSV boundary serializes those values as two-decimal-place text.
"""

import json
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Self

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

GENERATOR_CONTRACT_VERSION = "1.0.0"


class DatasetScale(StrEnum):
    TINY = "tiny"
    FULL = "full"


class GeneratorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    scale: DatasetScale
    seed: int = 20260901
    start_at: datetime
    end_at: datetime
    customers: int = Field(gt=0)
    products: int = Field(gt=0)
    orders: int = Field(gt=0)
    campaigns: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at and end_at must be timezone-aware")
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class TableDigest(BaseModel):
    table_name: str
    row_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DatasetManifest(BaseModel):
    dataset_id: str
    config_sha256: str
    seed: int
    scale: DatasetScale
    tables: tuple[TableDigest, ...]
    anomaly_manifest_path: Path


def load_generator_config(path: str | Path) -> GeneratorConfig:
    """Load and validate one non-empty YAML generator configuration mapping."""
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("generator configuration must be a non-empty YAML mapping")
    return GeneratorConfig.model_validate(raw)


def generator_config_sha256(config: GeneratorConfig) -> str:
    """Return the canonical UTF-8 SHA-256 hash for a generator configuration."""
    canonical_json = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical_json.encode("utf-8")).hexdigest()


def dataset_id_for_config(config: GeneratorConfig) -> str:
    """Return the versioned stable dataset identifier for a configuration."""
    config_sha256 = generator_config_sha256(config)
    identity = f"{GENERATOR_CONTRACT_VERSION}:{config_sha256}"
    return sha256(identity.encode("utf-8")).hexdigest()
