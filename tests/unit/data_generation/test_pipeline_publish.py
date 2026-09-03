"""Failure boundaries for publishing generated evidence snapshots."""

from pathlib import Path
from types import SimpleNamespace

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation import pipeline
from governed_analytics.data_generation.loader import TABLE_LOAD_ORDER
from governed_analytics.data_generation.models import DatasetManifest, load_generator_config


def _known_artifacts() -> list[str]:
    return [
        *(f"{name}.csv" for name in TABLE_LOAD_ORDER),
        "anomaly_manifest.json",
        "source_csv_digests.json",
        "dataset_manifest.json",
    ]


def _stage_new_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_stage(_: str, staging_root: Path) -> DatasetManifest:
        for name in _known_artifacts():
            (staging_root / name).write_bytes(f"new:{name}".encode())
        return DatasetManifest.model_construct()

    monkeypatch.setattr(pipeline, "_generate_and_load_staged", fake_stage)


def _write_prior_snapshot(output: Path, *, missing: str | None = None) -> dict[str, bytes]:
    output.mkdir()
    for name in _known_artifacts():
        if name != missing:
            (output / name).write_bytes(f"prior:{name}".encode())
    (output / "user-note.txt").write_text("do not touch", encoding="utf-8")
    return {name: (output / name).read_bytes() for name in _known_artifacts() if name != missing}


def test_generate_failure_keeps_prior_known_artifacts_and_removes_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "tiny"
    output.mkdir()
    known = _known_artifacts()
    for name in known:
        (output / name).write_bytes(f"prior:{name}".encode())
    (output / "user-note.txt").write_text("do not touch", encoding="utf-8")
    before = {name: (output / name).read_bytes() for name in known}
    config = load_generator_config("data/generator/tiny.yaml")
    frames = {name: pd.DataFrame() for name in TABLE_LOAD_ORDER}
    monkeypatch.setattr(
        pipeline,
        "_generated_frames",
        lambda _: (config, frames, SimpleNamespace(model_dump=lambda **_: {"truth": "ok"})),
    )

    def fake_csv(_: pd.DataFrame, path: Path, **__: object) -> str:
        path.write_text("new", encoding="utf-8")
        return "0" * 64

    monkeypatch.setattr(pipeline, "write_canonical_csv", fake_csv)
    monkeypatch.setattr(
        pipeline, "load_csvs", lambda _: (_ for _ in ()).throw(RuntimeError("load failed"))
    )

    with pytest.raises(RuntimeError, match="load failed"):
        pipeline.generate_and_load("data/generator/tiny.yaml", output)

    assert {name: (output / name).read_bytes() for name in known} == before
    assert (output / "user-note.txt").read_text(encoding="utf-8") == "do not touch"
    assert not list(tmp_path.glob(".tiny-staging-*"))


@pytest.mark.parametrize("failed_artifact", ("orders.csv", "dataset_manifest.json"))
def test_publish_replace_failure_rolls_back_known_snapshot_and_cleans_workdirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_artifact: str
) -> None:
    output = tmp_path / "tiny"
    missing = "campaign_attributions.csv"
    before = _write_prior_snapshot(output, missing=missing)
    _stage_new_snapshot(monkeypatch)
    real_replace = pipeline._replace_file
    failed = False

    def fail_once(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed
            and source_path.name == failed_artifact
            and source_path.parent.name.startswith(".tiny-staging-")
            and destination_path == output / failed_artifact
        ):
            failed = True
            raise OSError("injected publish failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(pipeline, "_replace_file", fail_once)

    with pytest.raises(OSError, match="injected publish failure"):
        pipeline.generate_and_load("data/generator/tiny.yaml", output)

    assert failed
    assert {name: (output / name).read_bytes() for name in before} == before
    assert not (output / missing).exists()
    assert (output / "user-note.txt").read_text(encoding="utf-8") == "do not touch"
    assert not list(tmp_path.glob(".tiny-staging-*"))
    assert not list(tmp_path.glob(".tiny-backup-*"))
