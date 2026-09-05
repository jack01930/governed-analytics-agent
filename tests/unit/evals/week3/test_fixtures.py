from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from governed_analytics.evals.week3 import fixtures
from governed_analytics.evals.week3.fixtures import freeze_week3_expected
from governed_analytics.evals.week3.suites import WEEK3_ORACLE_ROOT


def _open_fd_count() -> int:
    fd_root = Path("/dev/fd")
    if not fd_root.is_dir():
        fd_root = Path("/proc/self/fd")
    return len(os.listdir(fd_root))


def _close_if_open(file_descriptor: int) -> None:
    try:
        os.fstat(file_descriptor)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
    else:
        os.close(file_descriptor)


@pytest.mark.parametrize(
    "failing_positions",
    [(1,), (2,), (1, 2)],
    ids=["directory-fails", "parent-fails", "both-fail"],
)
def test_owned_staging_close_tracks_each_fd_and_recovers_without_growth(
    failing_positions: tuple[int, ...],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _open_fd_count()
    owned = fixtures._create_owned_staging(tmp_path)
    fixtures._clean_owned_staging(owned)
    assert not owned.directory_fd_closed
    assert not owned.parent_fd_closed
    assert not owned.closed
    real_close = os.close
    attempted: list[int] = []

    def failing_close(file_descriptor: int) -> None:
        if len(attempted) < 2:
            attempted.append(file_descriptor)
            if len(attempted) in failing_positions:
                raise OSError(
                    errno.EBADF,
                    f"injected descriptor close failure {len(attempted)}",
                )
        real_close(file_descriptor)

    monkeypatch.setattr(os, "close", failing_close)
    with pytest.raises(OSError, match="injected descriptor close failure") as error:
        owned.close()

    assert attempted == [owned.directory_fd, owned.parent_fd]
    assert f"failure {min(failing_positions)}" in str(error.value)
    assert owned.directory_fd_closed is (1 not in failing_positions)
    assert owned.parent_fd_closed is (2 not in failing_positions)
    assert not owned.closed

    monkeypatch.setattr(os, "close", real_close)
    owned.close()
    owned.close()

    assert owned.directory_fd_closed
    assert owned.parent_fd_closed
    assert owned.closed
    assert _open_fd_count() == baseline


@pytest.mark.parametrize(
    "failing_positions",
    [(1,), (2,), (1, 2)],
    ids=["directory-fails", "parent-fails", "both-fail"],
)
def test_owned_staging_creation_rollback_cleans_identity_despite_close_errors(
    failing_positions: tuple[int, ...],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _open_fd_count()
    real_close = os.close
    real_validate = fixtures._validate_owned_staging
    opened: list[int] = []
    attempted: list[int] = []
    open_directory = fixtures._open_directory_no_follow

    def tracking_open(path: Path) -> int:
        file_descriptor = open_directory(path)
        opened.append(file_descriptor)
        return file_descriptor

    def fail_validation(_owned: Any, *, require_path: bool = True) -> None:
        del require_path
        raise RuntimeError("injected construction failure")

    def failing_close(file_descriptor: int) -> None:
        if len(attempted) < 2:
            attempted.append(file_descriptor)
            if len(attempted) in failing_positions:
                raise OSError(errno.EBADF, "injected descriptor close failure")
        real_close(file_descriptor)

    monkeypatch.setattr(fixtures, "_open_directory_no_follow", tracking_open)
    monkeypatch.setattr(fixtures, "_validate_owned_staging", fail_validation)
    monkeypatch.setattr(os, "close", failing_close)
    try:
        with pytest.raises(RuntimeError, match=r"^injected construction failure$"):
            fixtures._create_owned_staging(tmp_path)

        assert attempted == [opened[1], opened[0]]
        assert not tuple(tmp_path.glob(".week3-expected-*"))
    finally:
        monkeypatch.setattr(os, "close", real_close)
        monkeypatch.setattr(fixtures, "_validate_owned_staging", real_validate)
        for file_descriptor in opened:
            _close_if_open(file_descriptor)

    assert _open_fd_count() == baseline


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
        assert staging.stat().st_mode & 0o777 == 0o700
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

    def create_target_at_publish_boundary(owned: Any, destination: Path) -> None:
        assert publish is not None
        destination.mkdir()
        (destination / "racer-owned.txt").write_text("preserve", encoding="utf-8")
        publish(owned, destination)

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


def test_pre_publish_source_swap_preserves_foreign_and_owned_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    owned_alias = tmp_path / "owned-staging-alias"

    async def swap_staging(staging: Path) -> tuple[str, ...]:
        (staging / "owned.txt").write_text("owned", encoding="utf-8")
        staging.rename(owned_alias)
        staging.mkdir(mode=0o700)
        (staging / "foreign.txt").write_text("foreign", encoding="utf-8")
        return ("W3K011.json",)

    monkeypatch.setattr(fixtures, "_freeze_to_staging", swap_staging)
    with pytest.raises(RuntimeError, match=r"^Week 3 staging directory identity changed$"):
        freeze_week3_expected(dataset="tiny", output_root=output)

    assert not output.exists()
    assert (owned_alias / "owned.txt").read_text(encoding="utf-8") == "owned"
    foreign = next(tmp_path.glob(".week3-expected-*"))
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign"


def test_collision_cleanup_does_not_delete_source_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    owned_alias = tmp_path / "owned-staging-alias"
    publish = fixtures._publish_staged_directory

    async def write_owned(staging: Path) -> tuple[str, ...]:
        (staging / "owned.txt").write_text("owned", encoding="utf-8")
        return ("W3K011.json",)

    def swap_source_then_collide(owned: Any, destination: Path) -> None:
        staging = owned.path
        staging.rename(owned_alias)
        staging.mkdir(mode=0o700)
        (staging / "foreign.txt").write_text("foreign", encoding="utf-8")
        destination.mkdir()
        (destination / "racer.txt").write_text("racer", encoding="utf-8")
        publish(owned, destination)

    monkeypatch.setattr(fixtures, "_freeze_to_staging", write_owned)
    monkeypatch.setattr(fixtures, "_publish_staged_directory", swap_source_then_collide)
    with pytest.raises(RuntimeError, match=r"^Week 3 staging directory identity changed$"):
        freeze_week3_expected(dataset="tiny", output_root=output)

    assert (output / "racer.txt").read_text(encoding="utf-8") == "racer"
    assert (owned_alias / "owned.txt").read_text(encoding="utf-8") == "owned"
    foreign = next(tmp_path.glob(".week3-expected-*"))
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign"


def test_native_publish_seam_swap_is_quarantined_without_exposing_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    owned_alias = tmp_path / "owned-staging-alias"
    rename_no_replace = fixtures._rename_path_no_replace
    swapped = False

    async def write_owned(staging: Path) -> tuple[str, ...]:
        (staging / "owned.txt").write_text("owned", encoding="utf-8")
        return ("W3K011.json",)

    def swap_at_native_seam(source: Path, destination: Path) -> None:
        nonlocal swapped
        if not swapped and source.name.startswith(".week3-expected-"):
            swapped = True
            source.rename(owned_alias)
            source.mkdir(mode=0o700)
            (source / "foreign.txt").write_text("foreign", encoding="utf-8")
        rename_no_replace(source, destination)

    monkeypatch.setattr(fixtures, "_freeze_to_staging", write_owned)
    monkeypatch.setattr(fixtures, "_rename_path_no_replace", swap_at_native_seam)
    with pytest.raises(RuntimeError, match=r"^Week 3 staging directory identity changed$"):
        freeze_week3_expected(dataset="tiny", output_root=output)

    assert not output.exists()
    assert (owned_alias / "owned.txt").read_text(encoding="utf-8") == "owned"
    quarantines = tuple(tmp_path.glob(".week3-quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "foreign.txt").read_text(encoding="utf-8") == "foreign"


def test_cleanup_entry_swap_preserves_foreign_and_owned_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    owned_alias = tmp_path / "owned-staging-alias"

    async def swap_then_fail(staging: Path) -> tuple[str, ...]:
        (staging / "owned.txt").write_text("owned", encoding="utf-8")
        staging.rename(owned_alias)
        staging.mkdir(mode=0o700)
        (staging / "foreign.txt").write_text("foreign", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(fixtures, "_freeze_to_staging", swap_then_fail)
    with pytest.raises(KeyboardInterrupt):
        freeze_week3_expected(dataset="tiny", output_root=output)

    assert not output.exists()
    assert (owned_alias / "owned.txt").read_text(encoding="utf-8") == "owned"
    foreign = next(tmp_path.glob(".week3-expected-*"))
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign"


def test_cleanup_delete_seam_revalidates_before_touching_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    owned_alias = tmp_path / "owned-staging-alias"
    remove_owned = fixtures._remove_owned_staging

    async def fail(staging: Path) -> tuple[str, ...]:
        (staging / "owned.txt").write_text("owned", encoding="utf-8")
        raise KeyboardInterrupt

    def swap_at_delete_seam(owned: Any) -> None:
        staging = owned.path
        staging.rename(owned_alias)
        staging.mkdir(mode=0o700)
        (staging / "foreign.txt").write_text("foreign", encoding="utf-8")
        remove_owned(owned)

    monkeypatch.setattr(fixtures, "_freeze_to_staging", fail)
    monkeypatch.setattr(fixtures, "_remove_owned_staging", swap_at_delete_seam)
    with pytest.raises(KeyboardInterrupt):
        freeze_week3_expected(dataset="tiny", output_root=output)

    assert not output.exists()
    assert (owned_alias / "owned.txt").read_text(encoding="utf-8") == "owned"
    foreign = next(tmp_path.glob(".week3-expected-*"))
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign"


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
