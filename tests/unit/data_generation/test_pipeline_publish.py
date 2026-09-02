"""Failure boundaries for publishing generated evidence snapshots."""

from pathlib import Path
from types import SimpleNamespace

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation import pipeline
from governed_analytics.data_generation.loader import TABLE_LOAD_ORDER
from governed_analytics.data_generation.models import load_generator_config


def test_generate_failure_keeps_prior_known_artifacts_and_removes_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "tiny"
    output.mkdir()
    known = [
        *(f"{name}.csv" for name in TABLE_LOAD_ORDER),
        "anomaly_manifest.json",
        "source_csv_digests.json",
        "dataset_manifest.json",
    ]
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
