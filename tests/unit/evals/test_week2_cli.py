from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from governed_analytics.evals import cli
from governed_analytics.evals.week2_runner import Week2RunError


def test_week2_fixture_never_constructs_api_client_or_requires_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls: list[object] = []
    run_calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: client_calls.append(kwargs))
    monkeypatch.setattr(cli, "_run_week2", lambda **kwargs: run_calls.append(kwargs))

    assert cli.main(["week2", "--dataset", "tiny", "--mode", "fixture"]) == 0
    assert client_calls == []
    assert len(run_calls) == 1


def test_week2_live_without_explicit_confirmation_is_rejected_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: client_calls.append(kwargs))

    assert cli.main(["week2", "--dataset", "tiny", "--mode", "live"]) == 2
    assert capsys.readouterr().err.strip() == "Live model calls require --live"
    assert client_calls == []


def test_week2_success_is_not_relabelled_failed_when_client_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []

    async def successful_run(**_kwargs: object) -> None:
        runs.append("published")

    class FailingCloseClient:
        async def close(self) -> None:
            raise RuntimeError("provider cleanup detail")

    monkeypatch.setattr(cli, "run_week2_evaluation", successful_run)

    cli._run_week2(mode="live", client=FailingCloseClient())  # type: ignore[arg-type]

    assert runs == ["published"]


def test_week2_run_failure_survives_a_second_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_run(**_kwargs: object) -> None:
        raise Week2RunError("sanitized run failure")

    class FailingCloseClient:
        async def close(self) -> None:
            raise RuntimeError("provider cleanup detail")

    monkeypatch.setattr(cli, "run_week2_evaluation", failed_run)

    with pytest.raises(Week2RunError, match="sanitized run failure"):
        cli._run_week2(mode="live", client=FailingCloseClient())  # type: ignore[arg-type]


def test_week2_summary_file_uses_the_report_returned_by_this_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = tmp_path / "pointers" / "week2.json"
    decoy = tmp_path / "historical-report.json"
    decoy.write_text('{"run_id":"historical-decoy"}', encoding="utf-8")
    returned = SimpleNamespace(
        run_id="current-run-sentinel",
        suite_manifest_sha256="a" * 64,
        safety_rejection_rate=Decimal("1"),
    )
    monkeypatch.setattr(cli, "_run_week2", lambda **_kwargs: returned)

    assert (
        cli.main(
            [
                "week2",
                "--dataset",
                "tiny",
                "--mode",
                "fixture",
                "--summary-file",
                str(summary),
            ]
        )
        == 0
    )
    assert json.loads(summary.read_text(encoding="utf-8")) == {
        "run_id": "current-run-sentinel",
        "safety_rejection_rate": "1",
        "suite_manifest_sha256": "a" * 64,
    }
    assert "historical-decoy" not in summary.read_text(encoding="utf-8")


def test_atomic_pointer_rejects_target_symlink_without_touching_referent(
    tmp_path: Path,
) -> None:
    referent = tmp_path / "referent"
    referent.write_text("unchanged", encoding="utf-8")
    target = tmp_path / "summary.json"
    target.symlink_to(referent)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(target, "replacement")

    assert referent.read_text(encoding="utf-8") == "unchanged"
    assert target.is_symlink()


def test_atomic_pointer_treats_parent_fsync_failure_after_replace_as_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original_fsync = os.fsync
    calls = 0

    def fail_parent_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("post-replace durability detail")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_parent_fsync)

    cli._atomic_write_text(target, "published")

    assert target.read_text(encoding="utf-8") == "published"


def test_atomic_pointer_treats_replace_wrapper_error_after_native_publish_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original_replace = os.replace

    def replace_then_raise(*args: object, **kwargs: object) -> None:
        original_replace(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("wrapper failed after native replace")

    monkeypatch.setattr(os, "replace", replace_then_raise)

    cli._atomic_write_text(target, "published")

    assert target.read_text(encoding="utf-8") == "published"


def test_atomic_pointer_rejects_foreign_target_created_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original_fsync = os.fsync
    injected = False

    def inject_foreign_after_staging(fd: int) -> None:
        nonlocal injected
        original_fsync(fd)
        if not injected:
            injected = True
            target.write_text("foreign", encoding="utf-8")

    monkeypatch.setattr(os, "fsync", inject_foreign_after_staging)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(target, "owned")

    assert target.read_text(encoding="utf-8") == "foreign"


def test_atomic_pointer_rejects_foreign_inode_after_replace_and_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "summary.json"
    original_fsync = os.fsync
    calls = 0

    def replace_target_during_parent_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.unlink()
            target.write_text("foreign", encoding="utf-8")
            raise OSError("foreign post-replace race")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", replace_target_during_parent_fsync)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(target, "owned")

    assert target.read_text(encoding="utf-8") == "foreign"


def test_atomic_pointer_rejects_precreated_temporary_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    referent = tmp_path / "referent"
    referent.write_text("unchanged", encoding="utf-8")
    (tmp_path / ".eval-pointer-fixed.tmp").symlink_to(referent)
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="fixed"))

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "summary.json", "owned")

    assert referent.read_text(encoding="utf-8") == "unchanged"


def test_atomic_pointer_aliases_share_one_thread_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "pointer.txt"
    (tmp_path / "unused").mkdir()
    alias = tmp_path / "unused" / ".." / "pointer.txt"
    first_entered = threading.Event()
    release_first = threading.Event()
    entries: list[str] = []
    original = cli._atomic_write_text_locked

    def blocked(binding: object, contents: str, **kwargs: object) -> None:
        entries.append(contents)
        if contents == "first":
            first_entered.set()
            assert release_first.wait(timeout=5)
        original(binding, contents, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_atomic_write_text_locked", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cli._atomic_write_text, target, "first")
        assert first_entered.wait(timeout=5)
        second = pool.submit(cli._atomic_write_text, alias, "second")
        assert not second.done()
        assert entries == ["first"]
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert entries == ["first", "second"]
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_pointer_fails_if_requested_parent_is_rebound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "pointers"
    parent.mkdir()
    moved = tmp_path / "moved"
    target = parent / "pointer.txt"
    original_replace = os.replace

    def replace_after_parent_rebind(*args: object, **kwargs: object) -> None:
        parent.rename(moved)
        parent.mkdir()
        original_replace(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace_after_parent_rebind)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(target, "owned")

    assert not target.exists()


def test_atomic_pointer_alias_writers_remain_complete_across_repeated_runs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pointer.txt"
    (tmp_path / "spare").mkdir()
    alias = tmp_path / "spare" / ".." / "pointer.txt"
    values = ("A" * 4096, "B" * 4096)

    for _ in range(12):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(cli._atomic_write_text, target, values[0]),
                pool.submit(cli._atomic_write_text, alias, values[1]),
            )
            for future in futures:
                future.result(timeout=5)
        assert target.read_text(encoding="utf-8") in values


def test_atomic_pointer_rejects_symlink_before_dotdot_component(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    safe.mkdir()
    linked = safe / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(linked / ".." / "pointer.txt", "unsafe")

    assert not (safe / "pointer.txt").exists()
    assert not (tmp_path / "pointer.txt").exists()


def test_atomic_pointer_preserves_foreign_leaf_swapped_after_bound_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "pointer.txt"
    original_read = os.read
    injected = False

    def read_then_swap(fd: int, size: int) -> bytes:
        nonlocal injected
        chunk = original_read(fd, size)
        if chunk == b"owned" and not injected:
            injected = True
            target.unlink()
            target.write_text("foreign", encoding="utf-8")
        return chunk

    monkeypatch.setattr(os, "read", read_then_swap)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(target, "owned")

    assert injected
    assert target.read_text(encoding="utf-8") == "foreign"


def test_atomic_pointer_rollback_rechecks_leaf_next_to_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "pointer.txt"
    original_read = os.read
    owned_reads = 0
    guard_calls = 0

    def read_then_swap_during_rollback(fd: int, size: int) -> bytes:
        nonlocal owned_reads
        chunk = original_read(fd, size)
        if chunk == b"owned":
            owned_reads += 1
            if owned_reads == 2:
                target.unlink()
                target.write_text("foreign", encoding="utf-8")
        return chunk

    def fail_postpublication_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise OSError("source changed")

    monkeypatch.setattr(os, "read", read_then_swap_during_rollback)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(
            target,
            "owned",
            publication_guard=fail_postpublication_guard,
        )

    assert guard_calls == 2
    assert owned_reads == 2
    assert target.read_text(encoding="utf-8") == "foreign"


def test_atomic_pointer_final_guard_cannot_swap_pointer_undetected(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pointer.txt"

    def swap_pointer() -> None:
        target.unlink()
        target.write_text("foreign", encoding="utf-8")

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(
            target,
            "owned",
            final_publication_guard=swap_pointer,
        )

    assert target.read_text(encoding="utf-8") == "foreign"


def test_atomic_pointer_final_checks_are_full_then_source_then_pointer_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "pointer.txt"
    events: list[str] = []
    original_full = cli._verify_bound_regular_file
    original_identity = cli._verify_bound_identity

    def record_full(*args: object, **kwargs: object) -> object:
        if args[1] == target.name and kwargs.get("held_fd") is not None:
            events.append("pointer-full")
        return original_full(*args, **kwargs)  # type: ignore[arg-type]

    def record_source() -> None:
        events.append("source-light")

    def record_identity(*args: object, **kwargs: object) -> None:
        events.append("pointer-light")
        original_identity(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_verify_bound_regular_file", record_full)
    monkeypatch.setattr(cli, "_verify_bound_identity", record_identity)

    cli._atomic_write_text(
        target,
        "owned",
        final_publication_guard=record_source,
    )

    assert events == ["pointer-full", "source-light", "pointer-light"]


def test_atomic_pointer_partial_temp_cleanup_never_unlinks_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="fixed-cleanup"))
    temporary = tmp_path / ".eval-pointer-fixed-cleanup.tmp"
    original_identity = cli._file_identity_at
    original_verify = cli._verify_bound_regular_file

    def identity_then_swap(directory_fd: int, name: str) -> tuple[int, int, int] | None:
        identity = original_identity(directory_fd, name)
        if name == temporary.name and identity is not None:
            temporary.unlink()
            temporary.write_text("foreign", encoding="utf-8")
        return identity

    def verify_then_swap(*args: object, **kwargs: object) -> object:
        result = original_verify(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] == temporary.name and kwargs.get("unlink_verified") is True:
            temporary.write_text("foreign", encoding="utf-8")
        return result

    monkeypatch.setattr(cli, "_file_identity_at", identity_then_swap)
    monkeypatch.setattr(cli, "_verify_bound_regular_file", verify_then_swap)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(
            tmp_path / "pointer.txt",
            "partial-owned",
            publication_guard=lambda: (_ for _ in ()).throw(OSError("stop")),
        )

    assert temporary.read_text(encoding="utf-8") == "foreign"


def test_atomic_pointer_cleans_owned_partial_temp_by_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="fixed-partial"))
    temporary = tmp_path / ".eval-pointer-fixed-partial.tmp"

    def fail_staging_fsync(_fd: int) -> None:
        raise OSError("staging failure")

    monkeypatch.setattr(os, "fsync", fail_staging_fsync)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "pointer.txt", "partial-owned")

    assert not temporary.exists()


def test_atomic_pointer_cleans_temp_when_initial_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="initial-fstat"))
    temporary = tmp_path / ".eval-pointer-initial-fstat.tmp"
    original_open = os.open
    original_fstat = os.fstat
    temporary_fds: list[int] = []
    failures = 0

    def track_open(path: object, *args: object, **kwargs: object) -> int:
        fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == temporary.name:
            temporary_fds.append(fd)
        return fd

    def fail_initial_fstat(fd: int) -> os.stat_result:
        nonlocal failures
        if temporary_fds and fd == temporary_fds[0] and failures == 0:
            failures += 1
            raise OSError("initial temp fstat failure")
        return original_fstat(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fstat", fail_initial_fstat)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "pointer.txt", "owned")

    assert failures == 1
    assert not temporary.exists()
    with pytest.raises(OSError):
        original_fstat(temporary_fds[0])


def test_atomic_pointer_persistent_initial_fstat_failure_closes_raw_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="persistent-fstat"))
    temporary = tmp_path / ".eval-pointer-persistent-fstat.tmp"
    original_open = os.open
    original_fstat = os.fstat
    temporary_fds: list[int] = []

    def track_open(path: object, *args: object, **kwargs: object) -> int:
        fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == temporary.name:
            temporary_fds.append(fd)
        return fd

    def fail_temp_fstat(fd: int) -> os.stat_result:
        if temporary_fds and fd == temporary_fds[0]:
            raise OSError("persistent temp fstat failure")
        return original_fstat(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fstat", fail_temp_fstat)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "pointer.txt", "owned")

    assert temporary.is_file()
    with pytest.raises(OSError):
        original_fstat(temporary_fds[0])


def test_atomic_pointer_initial_fstat_failure_preserves_foreign_temp_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="foreign-fstat"))
    temporary = tmp_path / ".eval-pointer-foreign-fstat.tmp"
    original_open = os.open
    original_fstat = os.fstat
    temporary_fds: list[int] = []
    injected = False

    def track_open(path: object, *args: object, **kwargs: object) -> int:
        fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == temporary.name:
            temporary_fds.append(fd)
        return fd

    def replace_before_failed_fstat(fd: int) -> os.stat_result:
        nonlocal injected
        if temporary_fds and fd == temporary_fds[0] and not injected:
            injected = True
            temporary.unlink()
            temporary.write_text("foreign", encoding="utf-8")
            raise OSError("initial temp fstat failure")
        return original_fstat(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fstat", replace_before_failed_fstat)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "pointer.txt", "owned")

    assert injected
    assert temporary.read_text(encoding="utf-8") == "foreign"
    with pytest.raises(OSError):
        original_fstat(temporary_fds[0])


def test_atomic_pointer_fdopen_failure_closes_and_unlinks_owned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="fdopen"))
    temporary = tmp_path / ".eval-pointer-fdopen.tmp"
    original_open = os.open
    original_fstat = os.fstat
    original_fdopen = os.fdopen
    temporary_fds: list[int] = []

    def track_open(path: object, *args: object, **kwargs: object) -> int:
        fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == temporary.name:
            temporary_fds.append(fd)
        return fd

    def fail_temp_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        if temporary_fds and fd == temporary_fds[0]:
            raise OSError("temp fdopen failure")
        return original_fdopen(fd, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fdopen", fail_temp_fdopen)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "pointer.txt", "owned")

    assert not temporary.exists()
    with pytest.raises(OSError):
        original_fstat(temporary_fds[0])


def test_atomic_pointer_parent_fstat_failure_closes_returned_directory_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open_directory = cli._open_directory_nofollow
    original_fstat = os.fstat
    parent_fds: list[int] = []

    def track_parent(path: Path, *, create_missing: bool) -> int:
        fd = original_open_directory(path, create_missing=create_missing)
        if create_missing and not parent_fds:
            parent_fds.append(fd)
        return fd

    def fail_parent_fstat(fd: int) -> os.stat_result:
        if parent_fds and fd == parent_fds[0]:
            raise OSError("parent fstat failure")
        return original_fstat(fd)

    monkeypatch.setattr(cli, "_open_directory_nofollow", track_parent)
    monkeypatch.setattr(os, "fstat", fail_parent_fstat)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(tmp_path / "pointer.txt", "owned")

    with pytest.raises(OSError):
        original_fstat(parent_fds[0])
