"""Small, non-configurable command line boundary for dataset evidence."""

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from governed_analytics.data_generation.anomalies import AnomalyManifest
from governed_analytics.data_generation.loader import TABLE_LOAD_ORDER
from governed_analytics.data_generation.models import DatasetManifest, DatasetScale
from governed_analytics.data_generation.pipeline import generate_and_load

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SCALE_CONFIGS = {
    DatasetScale.TINY.value: _REPOSITORY_ROOT / "data" / "generator" / "tiny.yaml",
    DatasetScale.FULL.value: _REPOSITORY_ROOT / "data" / "generator" / "full.yaml",
}


class _SourceDigest(BaseModel):
    """The source CSV evidence is deliberately as narrow as the database digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_name: str
    row_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


_SOURCE_DIGESTS = TypeAdapter(tuple[_SourceDigest, ...])


def build_parser() -> argparse.ArgumentParser:
    """Build the fixed public CLI surface without arbitrary file or DB switches."""
    parser = argparse.ArgumentParser(prog="governed-data")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "verify"):
        subcommand = subcommands.add_parser(command)
        subcommand.add_argument("--scale", required=True, choices=tuple(_SCALE_CONFIGS))
        subcommand.add_argument("--output", type=Path, default=Path("artifacts/datasets"))
    return parser


def config_path_for_scale(scale: str) -> Path:
    """Resolve only a contract-owned scale configuration, never a user path."""
    try:
        return _SCALE_CONFIGS[scale]
    except KeyError as error:
        raise ValueError(f"unsupported scale: {scale}") from error


def dataset_root(output_root: Path, scale: str) -> Path:
    """Keep every scale inside the caller-selected artifact root."""
    if scale not in _SCALE_CONFIGS:
        raise ValueError(f"unsupported scale: {scale}")
    return output_root / scale


def _print_manifest(manifest: DatasetManifest, manifest_path: Path) -> None:
    print(
        f"scale={manifest.scale} dataset={manifest.dataset_id} "
        f"config={manifest.config_sha256} seed={manifest.seed}"
    )
    for table in manifest.tables:
        print(f"table={table.table_name} rows={table.row_count} sha256={table.sha256}")
    print(f"manifest={manifest_path}")


def _load_dataset_manifest(path: Path) -> DatasetManifest:
    try:
        return DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
    except (OSError, ValidationError, ValueError) as error:
        raise ValueError("invalid expected dataset manifest") from error


def _load_anomaly_evidence(path: Path) -> Any:
    """Strict Pydantic validation returns canonical JSON-compatible anomaly evidence."""
    try:
        manifest = AnomalyManifest.model_validate_json(
            path.read_text(encoding="utf-8"), strict=True
        )
    except (OSError, ValidationError, ValueError) as error:
        raise ValueError("invalid expected anomaly manifest") from error
    return manifest.model_dump(mode="json")


def _load_source_evidence(path: Path) -> tuple[_SourceDigest, ...]:
    try:
        evidence = _SOURCE_DIGESTS.validate_json(path.read_text(encoding="utf-8"), strict=True)
    except (OSError, ValidationError, ValueError) as error:
        raise ValueError("invalid expected source digest evidence") from error
    if tuple(item.table_name for item in evidence) != TABLE_LOAD_ORDER:
        raise ValueError("invalid expected source digest evidence")
    return evidence


def _compare_manifests(expected: DatasetManifest, actual: DatasetManifest) -> list[str]:
    differences: list[str] = []
    for field in ("dataset_id", "config_sha256", "seed", "scale"):
        if getattr(expected, field) != getattr(actual, field):
            differences.append(f"{field} mismatch")
    if len(expected.tables) != len(TABLE_LOAD_ORDER) or len(actual.tables) != len(TABLE_LOAD_ORDER):
        differences.append("table manifest length mismatch")
    expected_by_name = {table.table_name: table for table in expected.tables}
    actual_by_name = {table.table_name: table for table in actual.tables}
    for table_name in TABLE_LOAD_ORDER:
        expected_table = expected_by_name.get(table_name)
        actual_table = actual_by_name.get(table_name)
        if expected_table != actual_table:
            differences.append(f"table mismatch: {table_name}")
    if tuple(table.table_name for table in expected.tables) != TABLE_LOAD_ORDER:
        differences.append("expected table order mismatch")
    if tuple(table.table_name for table in actual.tables) != TABLE_LOAD_ORDER:
        differences.append("actual table order mismatch")
    return differences


def _generate(scale: str, output_root: Path) -> int:
    root = dataset_root(output_root, scale)
    manifest = generate_and_load(config_path_for_scale(scale), root)
    _print_manifest(manifest, root / "dataset_manifest.json")
    return 0


def _verify(scale: str, output_root: Path) -> int:
    expected_root = dataset_root(output_root, scale)
    expected_manifest_path = expected_root / "dataset_manifest.json"
    if not expected_manifest_path.is_file():
        print("missing expected dataset manifest")
        return 2
    try:
        expected = _load_dataset_manifest(expected_manifest_path)
        expected_anomalies = _load_anomaly_evidence(expected_root / "anomaly_manifest.json")
        expected_source = _load_source_evidence(expected_root / "source_csv_digests.json")
    except ValueError as error:
        print(str(error))
        return 2

    # TemporaryDirectory is rooted beside the evidence, hence uses its filesystem and is removed
    # even when loading or comparison fails.  No user-owned artifact directory is ever deleted.
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{scale}-verify-", dir=output_root) as temporary:
        actual_root = Path(temporary) / scale
        actual = generate_and_load(config_path_for_scale(scale), actual_root)
        try:
            actual_anomalies = _load_anomaly_evidence(actual_root / "anomaly_manifest.json")
            actual_source = _load_source_evidence(actual_root / "source_csv_digests.json")
        except ValueError:
            print("generated evidence is invalid")
            return 1

        differences = _compare_manifests(expected, actual)
        if expected_anomalies != actual_anomalies:
            differences.append("anomaly manifest mismatch")
        if expected_source != actual_source:
            differences.append("source digest evidence mismatch")
        if differences:
            for difference in differences:
                print(difference)
            return 1
    print(f"verification succeeded: scale={scale} manifest={expected_manifest_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a fixed generation or regeneration-verification operation."""
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            return _generate(arguments.scale, arguments.output)
        return _verify(arguments.scale, arguments.output)
    except Exception:  # CLI errors must not leak connection URLs or environment values.
        print("data operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
