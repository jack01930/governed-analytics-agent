from __future__ import annotations

import json
import os
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
