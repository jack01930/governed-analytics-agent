from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from governed_analytics.evals.week3 import fixtures
from governed_analytics.evals.week3.fixtures import freeze_week3_expected
from governed_analytics.evals.week3.suites import WEEK3_ORACLE_ROOT


def test_freezer_accepts_only_tiny_before_database_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tiny"):
        freeze_week3_expected(dataset="large", output_root=tmp_path / "expected")  # type: ignore[arg-type]


def test_existing_output_is_rejected_before_database_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    output.mkdir()

    async def database_must_not_be_touched(_staging: Path) -> tuple[str, ...]:
        raise AssertionError("database was touched")

    monkeypatch.setattr(fixtures, "_freeze_to_staging", database_must_not_be_touched)
    with pytest.raises(FileExistsError):
        freeze_week3_expected(dataset="tiny", output_root=output)
    assert tuple(output.iterdir()) == ()


def test_freeze_failure_removes_owned_staging_and_leaves_no_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"

    async def fail_after_partial_write(staging: Path) -> tuple[str, ...]:
        (staging / "partial.json").write_text("{}", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(fixtures, "_freeze_to_staging", fail_after_partial_write)
    with pytest.raises(KeyboardInterrupt):
        freeze_week3_expected(dataset="tiny", output_root=output)
    assert not output.exists()
    assert not tuple(tmp_path.glob(".week3-expected-*"))


def test_successful_freeze_publishes_exactly_once_as_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    names = tuple(f"W3K{index:03d}.json" for index in range(1, 20))

    async def write_complete_staging(staging: Path) -> tuple[str, ...]:
        for name in names:
            (staging / name).write_text("{}", encoding="utf-8")
        return names

    monkeypatch.setattr(fixtures, "_freeze_to_staging", write_complete_staging)
    assert freeze_week3_expected(dataset="tiny", output_root=output) == tuple(
        output / name for name in names
    )
    assert {path.name for path in output.iterdir()} == set(names)
    with pytest.raises(FileExistsError):
        freeze_week3_expected(dataset="tiny", output_root=output)


def test_atomic_publish_preserves_target_created_at_publication_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    names = tuple(f"W3K{index:03d}.json" for index in range(1, 20))

    async def write_complete_staging(staging: Path) -> tuple[str, ...]:
        for name in names:
            (staging / name).write_text("{}", encoding="utf-8")
        return names

    publish = getattr(fixtures, "_publish_staged_directory", None)

    def create_target_at_publish_boundary(staging: Path, destination: Path) -> None:
        assert publish is not None
        destination.mkdir()
        (destination / "racer-owned.txt").write_text("preserve", encoding="utf-8")
        publish(staging, destination)

    monkeypatch.setattr(fixtures, "_freeze_to_staging", write_complete_staging)
    monkeypatch.setattr(
        fixtures,
        "_publish_staged_directory",
        create_target_at_publish_boundary,
        raising=False,
    )
    with pytest.raises(FileExistsError):
        freeze_week3_expected(dataset="tiny", output_root=output)

    assert (output / "racer-owned.txt").read_text(encoding="utf-8") == "preserve"
    assert not tuple(tmp_path.glob(".week3-expected-*"))


@pytest.mark.parametrize("symlink_kind", ["external_parent", "oracle_root", "oracle_leaf"])
def test_oracle_symlinks_are_rejected_before_database_access(
    symlink_kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_parent = tmp_path / "real-parent"
    real_root = real_parent / "oracle"
    real_parent.mkdir()
    shutil.copytree(WEEK3_ORACLE_ROOT, real_root)

    if symlink_kind == "external_parent":
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        oracle_root = linked_parent / "oracle"
    elif symlink_kind == "oracle_root":
        oracle_root = tmp_path / "oracle-link"
        oracle_root.symlink_to(real_root, target_is_directory=True)
    else:
        oracle_root = real_root
        victim = oracle_root / "W3K011.sql"
        source = oracle_root / "W3K012.sql"
        victim.unlink()
        victim.symlink_to(source)

    async def database_must_not_be_touched(_staging: Path) -> tuple[str, ...]:
        raise AssertionError("database was touched")

    monkeypatch.setattr(fixtures, "WEEK3_ORACLE_ROOT", oracle_root)
    monkeypatch.setattr(fixtures, "_freeze_to_staging", database_must_not_be_touched)
    with pytest.raises(ValueError, match=r"^Week 3 Oracle inventory is unavailable$"):
        freeze_week3_expected(dataset="tiny", output_root=tmp_path / "expected")


def test_oracle_inventory_is_exact_before_database_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    oracle_root = tmp_path / "oracle"
    shutil.copytree(WEEK3_ORACLE_ROOT, oracle_root)
    (oracle_root / "orphan.sql").write_text("select 1", encoding="utf-8")

    async def database_must_not_be_touched(_staging: Path) -> tuple[str, ...]:
        raise AssertionError("database was touched")

    monkeypatch.setattr(fixtures, "WEEK3_ORACLE_ROOT", oracle_root)
    monkeypatch.setattr(fixtures, "_freeze_to_staging", database_must_not_be_touched)
    with pytest.raises(ValueError, match=r"^Week 3 Oracle inventory is unavailable$"):
        freeze_week3_expected(dataset="tiny", output_root=tmp_path / "expected")


def test_freezer_module_has_no_live_model_client_dependency() -> None:
    source = Path(fixtures.__file__ or "").read_text(encoding="utf-8")
    assert "AsyncOpenAI" not in source
    assert "deepseek" not in source.casefold()
