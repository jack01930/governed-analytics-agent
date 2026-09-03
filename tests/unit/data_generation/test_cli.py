"""Behavioural tests for the deliberately narrow data evidence CLI."""

import json
from pathlib import Path

import pytest

from governed_analytics.data_generation import cli
from governed_analytics.data_generation.loader import TABLE_LOAD_ORDER
from governed_analytics.data_generation.models import DatasetManifest, DatasetScale, TableDigest


def _manifest(scale: DatasetScale = DatasetScale.TINY, suffix: str = "a") -> DatasetManifest:
    return DatasetManifest(
        dataset_id=("a" if suffix == "a" else "b") * 64,
        config_sha256=("c" if suffix == "a" else "d") * 64,
        seed=20260901,
        scale=scale,
        tables=tuple(
            TableDigest(table_name=name, row_count=index, sha256=f"{index:064x}")
            for index, name in enumerate(TABLE_LOAD_ORDER, start=1)
        ),
        anomaly_manifest_path=Path("anomaly_manifest.json"),
    )


def _write_evidence(root: Path, manifest: DatasetManifest, *, source_suffix: str = "a") -> None:
    root.mkdir(parents=True)
    (root / "dataset_manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    # The production validator deliberately requires eight records; tests mock its loader.
    (root / "anomaly_manifest.json").write_text('{"evidence":"same"}\n', encoding="utf-8")
    (root / "source_csv_digests.json").write_text(
        json.dumps(
            [
                {"table_name": name, "row_count": index, "sha256": f"{index:064x}"}
                for index, name in enumerate(TABLE_LOAD_ORDER, start=1)
            ]
        ),
        encoding="utf-8",
    )


def test_parser_only_admits_fixed_command_surface() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["generate", "--scale", "tiny"]).scale == "tiny"
    with pytest.raises(SystemExit):
        parser.parse_args(["generate", "--scale", "other"])
    with pytest.raises(SystemExit):
        parser.parse_args(["generate", "--scale", "tiny", "--config", "x"])


def test_scale_paths_are_repo_fixed_and_output_is_scoped(tmp_path: Path) -> None:
    assert cli.config_path_for_scale("tiny").name == "tiny.yaml"
    assert cli.dataset_root(tmp_path, "full") == tmp_path / "full"
    with pytest.raises(ValueError, match="unsupported scale"):
        cli.config_path_for_scale("../../x")


def test_generate_calls_pipeline_and_prints_nonsecret_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def fake_generate(config_path: Path, output_path: Path) -> DatasetManifest:
        captured.update(config_path=config_path, output_path=output_path)
        return _manifest()

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    assert cli.main(["generate", "--scale", "tiny", "--output", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert captured["output_path"] == tmp_path / "tiny"
    assert "scale=tiny" in output and "dataset=" + "a" * 64 in output
    assert "password" not in output.lower() and "postgresql" not in output.lower()


@pytest.mark.parametrize(
    ("kind", "expected", "message"),
    [
        ("config", _manifest(suffix="expected"), "config_sha256 mismatch"),
        ("table", _manifest(suffix="expected"), "table mismatch: categories"),
        ("anomaly", _manifest(), "anomaly manifest mismatch"),
        ("source", _manifest(), "source digest evidence mismatch"),
    ],
)
def test_verify_reports_each_evidence_difference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    expected: DatasetManifest,
    message: str,
) -> None:
    expected_root = tmp_path / "tiny"
    _write_evidence(expected_root, expected)
    generated = _manifest()
    if kind == "table":
        base = _manifest(suffix="expected")
        generated = base.model_copy(
            update={
                "tables": (
                    base.tables[0].model_copy(update={"sha256": "f" * 64}),
                    *base.tables[1:],
                )
            }
        )

    def fake_generate(_: Path, output: Path) -> DatasetManifest:
        output.mkdir(parents=True)
        (output / "anomaly_manifest.json").write_text(
            '{"evidence":"changed"}\n' if kind == "anomaly" else '{"evidence":"same"}\n',
            encoding="utf-8",
        )
        source = json.loads((expected_root / "source_csv_digests.json").read_text())
        if kind == "source":
            source[0]["sha256"] = "f" * 64
        (output / "source_csv_digests.json").write_text(json.dumps(source), encoding="utf-8")
        return generated

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    monkeypatch.setattr(cli, "_load_anomaly_evidence", lambda path: json.loads(path.read_text()))
    assert cli.main(["verify", "--scale", "tiny", "--output", str(tmp_path)]) == 1
    assert message in capsys.readouterr().out


def test_verify_success_keeps_expected_and_removes_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected_root = tmp_path / "tiny"
    expected = _manifest()
    _write_evidence(expected_root, expected)
    before = {path.name: path.read_bytes() for path in expected_root.iterdir()}

    def fake_generate(_: Path, output: Path) -> DatasetManifest:
        output.mkdir(parents=True)
        (output / "anomaly_manifest.json").write_text('{"evidence":"same"}\n', encoding="utf-8")
        (output / "source_csv_digests.json").write_bytes(
            (expected_root / "source_csv_digests.json").read_bytes()
        )
        return expected

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    monkeypatch.setattr(cli, "_load_anomaly_evidence", lambda path: json.loads(path.read_text()))
    assert cli.main(["verify", "--scale", "tiny", "--output", str(tmp_path)]) == 0
    assert "verification succeeded" in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in expected_root.iterdir()} == before
    assert not list(tmp_path.glob(".tiny-verify-*"))


def test_verify_rejects_missing_or_invalid_expected_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def fake_generate(_: Path, __: Path) -> DatasetManifest:
        nonlocal calls
        calls += 1
        return _manifest()

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    assert cli.main(["verify", "--scale", "tiny", "--output", str(tmp_path)]) == 2
    assert "missing expected dataset manifest" in capsys.readouterr().out
    root = tmp_path / "tiny"
    root.mkdir()
    (root / "dataset_manifest.json").write_text("not json", encoding="utf-8")
    assert cli.main(["verify", "--scale", "tiny", "--output", str(tmp_path)]) == 2
    assert "invalid expected dataset manifest" in capsys.readouterr().out
    assert calls == 0


@pytest.mark.parametrize("kind", ("dataset", "anomaly", "source"))
def test_verify_rejects_invalid_expected_evidence_before_generation_and_keeps_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    root = tmp_path / "tiny"
    _write_evidence(root, _manifest())
    if kind == "dataset":
        (root / "dataset_manifest.json").write_text(
            '{"secret":"postgresql://u:pw@db"}', encoding="utf-8"
        )
    elif kind == "anomaly":
        (root / "anomaly_manifest.json").write_text('{"unexpected": true}', encoding="utf-8")
    else:
        (root / "source_csv_digests.json").write_text(
            '[{"table_name":"categories","row_count":-1,"sha256":"not-a-hash"}]',
            encoding="utf-8",
        )
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    calls = 0

    def fake_generate(_: Path, __: Path) -> DatasetManifest:
        nonlocal calls
        calls += 1
        raise AssertionError("generation must not start")

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    if kind == "source":
        monkeypatch.setattr(cli, "_load_anomaly_evidence", lambda _: {})
    assert cli.main(["verify", "--scale", "tiny", "--output", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "postgresql://u:pw@db" not in captured.out + captured.err
    assert calls == 0
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert not list(tmp_path.glob(".tiny-verify-*"))


def test_operation_errors_are_stably_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "postgresql://user:p%40ssword@host/db"

    def fake_generate(_: Path, __: Path) -> DatasetManifest:
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    assert cli.main(["generate", "--scale", "tiny", "--output", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "data operation failed"
    assert (
        secret not in captured.out + captured.err
        and "p%40ssword" not in captured.out + captured.err
    )


@pytest.mark.parametrize("outcome", ("mismatch", "invalid_actual", "generate_error"))
def test_verify_preserves_all_expected_evidence_and_cleans_temp_on_non_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    expected_root = tmp_path / "tiny"
    expected = _manifest()
    _write_evidence(expected_root, expected)
    before = {path.name: path.read_bytes() for path in expected_root.iterdir()}

    def fake_generate(_: Path, output: Path) -> DatasetManifest:
        output.mkdir(parents=True)
        if outcome == "generate_error":
            raise RuntimeError("postgresql://u:secret@db")
        (output / "anomaly_manifest.json").write_text(
            "{}" if outcome == "invalid_actual" else '{"evidence":"same"}', encoding="utf-8"
        )
        (output / "source_csv_digests.json").write_bytes(
            (expected_root / "source_csv_digests.json").read_bytes()
        )
        return _manifest(suffix="expected") if outcome == "mismatch" else expected

    monkeypatch.setattr(cli, "generate_and_load", fake_generate)
    monkeypatch.setattr(
        cli,
        "_load_anomaly_evidence",
        lambda path: (
            json.loads(path.read_text())
            if path.read_text() != "{}"
            else (_ for _ in ()).throw(ValueError())
        ),
    )
    assert cli.main(["verify", "--scale", "tiny", "--output", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.out + captured.err
    assert {path.name: path.read_bytes() for path in expected_root.iterdir()} == before
    assert not list(tmp_path.glob(".tiny-verify-*"))
