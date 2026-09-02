"""Orchestrate deterministic generation, evidence artifacts and bounded loading."""

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


def generate_and_load(config_path: str | Path, output_root: Path) -> DatasetManifest:
    """Generate a complete dataset, atomically load it, and emit stable evidence artifacts."""
    config, frames, anomaly_manifest = _generated_frames(config_path)
    output_root.mkdir(parents=True, exist_ok=True)
    source_digests: list[dict[str, int | str]] = []
    csv_paths: dict[str, Path] = {}
    for table_name in TABLE_LOAD_ORDER:
        spec = TABLE_SPECS[table_name]
        frame = frames[table_name]
        csv_path = output_root / f"{table_name}.csv"
        digest = write_canonical_csv(
            frame,
            csv_path,
            sort_by=spec.sort_by,
            columns=spec.columns,
        )
        csv_paths[table_name] = csv_path
        source_digests.append(
            {"table_name": table_name, "row_count": len(frame), "sha256": digest}
        )

    write_canonical_json(
        anomaly_manifest.model_dump(mode="json"), output_root / "anomaly_manifest.json"
    )
    write_canonical_json(source_digests, output_root / "source_csv_digests.json")
    database_digests = load_csvs(csv_paths)
    manifest = DatasetManifest(
        dataset_id=dataset_id_for_config(config),
        config_sha256=generator_config_sha256(config),
        seed=config.seed,
        scale=config.scale,
        tables=database_digests,
        anomaly_manifest_path=Path("anomaly_manifest.json"),
    )
    write_canonical_json(manifest.model_dump(mode="json"), output_root / "dataset_manifest.json")
    return manifest
