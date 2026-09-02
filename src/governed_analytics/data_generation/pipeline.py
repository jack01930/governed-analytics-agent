"""Orchestrate deterministic generation, evidence artifacts and bounded loading."""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]

from governed_analytics.data_generation.anomalies import AnomalyManifest, inject_anomalies
from governed_analytics.data_generation.dimensions import (
    generate_campaigns,
    generate_categories,
    generate_customers,
    generate_products,
)
from governed_analytics.data_generation.facts import generate_base_facts
from governed_analytics.data_generation.loader import TABLE_LOAD_ORDER, TABLE_SPECS, load_csvs
from governed_analytics.data_generation.models import (
    DatasetManifest,
    GeneratorConfig,
    dataset_id_for_config,
    generator_config_sha256,
    load_generator_config,
)
from governed_analytics.data_generation.writer import write_canonical_csv, write_canonical_json

_PUBLISHED_ARTIFACTS = (
    *(f"{table_name}.csv" for table_name in TABLE_LOAD_ORDER),
    "anomaly_manifest.json",
    "source_csv_digests.json",
    "dataset_manifest.json",
)
_BACKUP_CHUNK_SIZE = 1024 * 1024


def _generated_frames(
    config_path: str | Path,
) -> tuple[GeneratorConfig, Mapping[str, pd.DataFrame], AnomalyManifest]:
    config = load_generator_config(config_path)
    mutations = inject_anomalies(config, generate_base_facts(config))
    frames = {
        "categories": generate_categories(),
        "customers": generate_customers(config),
        "products": generate_products(config),
        "marketing_campaigns": generate_campaigns(config),
        "orders": mutations.orders,
        "order_items": mutations.order_items,
        "payments": mutations.payments,
        "refunds": mutations.refunds,
        "inventory_snapshots": mutations.inventory_snapshots,
        "web_sessions": mutations.web_sessions,
        "campaign_attributions": mutations.campaign_attributions,
        "pipeline_runs": mutations.pipeline_runs,
    }
    return (config, frames, mutations.manifest)


def _generate_and_load_staged(config_path: str | Path, staging_root: Path) -> DatasetManifest:
    """Build/load a full snapshot in a private sibling directory before publication."""
    config, frames, anomaly_manifest = _generated_frames(config_path)
    staging_root.mkdir(parents=True, exist_ok=True)
    source_digests: list[dict[str, int | str]] = []
    csv_paths: dict[str, Path] = {}
    for table_name in TABLE_LOAD_ORDER:
        spec = TABLE_SPECS[table_name]
        frame = frames[table_name]
        csv_path = staging_root / f"{table_name}.csv"
        digest = write_canonical_csv(
            frame,
            csv_path,
            sort_by=spec.sort_by,
            columns=spec.columns,
        )
        csv_paths[table_name] = csv_path
        source_digests.append({"table_name": table_name, "row_count": len(frame), "sha256": digest})

    write_canonical_json(
        anomaly_manifest.model_dump(mode="json"), staging_root / "anomaly_manifest.json"
    )
    write_canonical_json(source_digests, staging_root / "source_csv_digests.json")
    database_digests = load_csvs(csv_paths)
    manifest = DatasetManifest(
        dataset_id=dataset_id_for_config(config),
        config_sha256=generator_config_sha256(config),
        seed=config.seed,
        scale=config.scale,
        tables=database_digests,
        anomaly_manifest_path=Path("anomaly_manifest.json"),
    )
    write_canonical_json(manifest.model_dump(mode="json"), staging_root / "dataset_manifest.json")
    return manifest


def _copy_backup(source: Path, destination: Path) -> None:
    """Copy one known artifact without retaining a whole full-scale CSV in memory."""
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while chunk := input_file.read(_BACKUP_CHUNK_SIZE):
            output_file.write(chunk)


def _replace_file(source: Path, destination: Path) -> None:
    """Small seam for publication fault-injection tests."""
    os.replace(source, destination)


def _backup_known_artifacts(output_root: Path, backup_root: Path) -> dict[str, bool]:
    """Return exact pre-publish existence state and byte backups for known files only."""
    prior_exists: dict[str, bool] = {}
    for artifact_name in _PUBLISHED_ARTIFACTS:
        target = output_root / artifact_name
        if target.exists() and not target.is_file():
            raise RuntimeError("known artifact target is not a regular file")
        prior_exists[artifact_name] = target.is_file()
        if target.is_file():
            _copy_backup(target, backup_root / artifact_name)
    return prior_exists


def _restore_known_artifacts(
    output_root: Path, backup_root: Path, prior_exists: Mapping[str, bool]
) -> None:
    """Restore only the fixed publication set after a caught publish failure."""
    for artifact_name in _PUBLISHED_ARTIFACTS:
        target = output_root / artifact_name
        if prior_exists[artifact_name]:
            _replace_file(backup_root / artifact_name, target)
        elif target.exists():
            target.unlink()


def _publish_staged_snapshot(staging_root: Path, output_root: Path) -> None:
    """Publish staged files and recover the pre-call snapshot on any caught replace failure."""
    with tempfile.TemporaryDirectory(
        prefix=f".{output_root.name}-backup-", dir=output_root.parent
    ) as temporary:
        backup_root = Path(temporary)
        prior_exists = _backup_known_artifacts(output_root, backup_root)
        try:
            for artifact_name in _PUBLISHED_ARTIFACTS:
                _replace_file(staging_root / artifact_name, output_root / artifact_name)
        except OSError as publish_error:
            try:
                _restore_known_artifacts(output_root, backup_root, prior_exists)
            except OSError as rollback_error:
                raise RuntimeError("publication rollback failed") from rollback_error
            raise publish_error


def generate_and_load(config_path: str | Path, output_root: Path) -> DatasetManifest:
    """Publish only a complete staged dataset without deleting user-owned files."""
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_root.name}-staging-", dir=output_root.parent
    ) as temporary:
        staging_root = Path(temporary)
        manifest = _generate_and_load_staged(config_path, staging_root)
        output_root.mkdir(parents=True, exist_ok=True)
        _publish_staged_snapshot(staging_root, output_root)
        return manifest
