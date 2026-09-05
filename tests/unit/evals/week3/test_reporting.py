from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from governed_analytics.agent.contracts import (
    BehaviorAction,
    BehaviorReasonCode,
    FinalStatus,
    StopReason,
)
from governed_analytics.evals.week3 import reporting
from governed_analytics.evals.week3.models import (
    BehaviorScore,
    BudgetScore,
    SuiteScore,
    ToolScore,
    Week3CaseResult,
    Week3RunReport,
    derive_executed_manifest_sha256,
)
from governed_analytics.evals.week3.reporting import (
    cancel_week3_report_reservation,
    reserve_week3_report,
    write_week3_report,
)


def _report(run_id: str) -> Week3RunReport:
    case = Week3CaseResult(
        case_id="W3K001",
        cohort="known",
        suite="behavior",
        expected_behavior=BehaviorAction.CLARIFY,
        expected_final_status=FinalStatus.CLARIFICATION_REQUIRED,
        expected_stop_reason=StopReason.MISSING_REQUIRED_FIELDS,
        observed_final_status=FinalStatus.CLARIFICATION_REQUIRED,
        observed_stop_reason=StopReason.MISSING_REQUIRED_FIELDS,
        observed_behavior=BehaviorAction.CLARIFY,
        observed_behavior_reason=BehaviorReasonCode.MISSING_METRIC,
        suite_score=SuiteScore(suite="behavior", conformant=True),
        behavior_score=BehaviorScore(
            action_conformant=True,
            reason_conformant=True,
            missing_fields_conformant=True,
            conformant=True,
        ),
        tool_score=ToolScore(
            required_present=True, forbidden_absent=True, sequence_conformant=True, conformant=True
        ),
        budget_score=BudgetScore(
            conformant=True,
            action_loops=0,
            llm_calls=0,
            tool_calls=0,
            execute_calls=0,
            profile_calls=0,
            repair_count=0,
            input_tokens=0,
            output_tokens=0,
            committed_cost_cny=Decimal("0"),
            soft_cap_reached=False,
            max_action_loops=1,
            max_llm_calls=1,
            max_tool_calls=1,
            max_execute_calls=1,
            max_profile_calls=1,
            max_repairs=1,
            expected_repair_count=0,
            hard_cost_cny=Decimal("1"),
        ),
    )
    overall = "a" * 64
    return Week3RunReport(
        mode="fixture",
        overall_manifest_sha256=overall,
        known_cohort_sha256="b" * 64,
        heldout_cohort_sha256="c" * 64,
        executed_manifest_sha256=derive_executed_manifest_sha256(
            overall_manifest_sha256=overall, mode="fixture", case_ids=("W3K001",)
        ),
        report_scope="partial_test",
        run_id=run_id,
        cases=(case,),
    )


def test_report_reservation_rejects_duplicate_run_directory(tmp_path: Path) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="duplicate",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    try:
        with pytest.raises(FileExistsError):
            reserve_week3_report(
                tmp_path,
                mode="fixture",
                run_id="duplicate",
                timestamp=datetime(2026, 9, 4, tzinfo=UTC),
            )
    finally:
        cancel_week3_report_reservation(reservation)


def test_report_is_atomic_and_historical_output_is_never_overwritten(tmp_path: Path) -> None:
    timestamp = datetime(2026, 9, 4, tzinfo=UTC)
    reservation = reserve_week3_report(
        tmp_path, mode="fixture", run_id="historical", timestamp=timestamp
    )
    published = write_week3_report(reservation, _report("historical"))
    original = published.report_json.read_bytes()

    with pytest.raises(FileExistsError):
        reserve_week3_report(tmp_path, mode="fixture", run_id="historical", timestamp=timestamp)

    assert published.report_json.read_bytes() == original
    assert not tuple((tmp_path / "fixture").glob(".*-staging-reservation"))


def test_every_published_file_excludes_sensitive_payload_shapes(tmp_path: Path) -> None:
    sentinel = "ROW_SENTINEL_SELECT_SK_ENDPOINT"
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="sanitized",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    published = write_week3_report(reservation, _report("sanitized"))

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in published.report_dir.rglob("*")
        if path.is_file()
    ).casefold()
    assert sentinel.casefold() not in combined
    assert "select " not in combined
    assert "sk-" not in combined
    assert "endpoint" not in combined


def test_report_publish_failure_cleans_only_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="write-failure",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )

    def fail_write(_directory_fd: int, _name: str, _contents: str) -> None:
        raise OSError("sensitive provider diagnostic")

    monkeypatch.setattr(reporting, "_write_text_at", fail_write)
    with pytest.raises(OSError):
        write_week3_report(reservation, _report("write-failure"))

    assert not reservation.staging_dir.exists()
    assert not reservation.final_dir.exists()


def test_report_reservation_rejects_symlink_in_parent_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        reserve_week3_report(
            linked,
            mode="fixture",
            run_id="parent-symlink",
            timestamp=datetime(2026, 9, 4, tzinfo=UTC),
        )


def test_report_file_symlink_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="file-symlink",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    target = tmp_path / "foreign"
    target.write_text("unchanged", encoding="utf-8")
    original = reporting._write_text_at
    injected = False

    def inject(directory_fd: int, name: str, contents: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.symlink(target, "report.json", dir_fd=directory_fd)
        original(directory_fd, name, contents)

    monkeypatch.setattr(reporting, "_write_text_at", inject)
    with pytest.raises(FileExistsError):
        write_week3_report(reservation, _report("file-symlink"))

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert not reservation.final_dir.exists()
    assert not reservation.staging_dir.exists()


def test_source_swap_at_native_publish_boundary_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="source-swap",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original = reporting.__dict__["_native_publish_owned"]
    swapped = False

    def swap_then_publish(active_reservation: object) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            source = reservation.staging_dir
            owned = source.with_name(".owned-moved")
            source.rename(owned)
            source.mkdir()
        original(active_reservation)

    monkeypatch.setattr(reporting, "_native_publish_owned", swap_then_publish)
    with pytest.raises(OSError, match="publication failed"):
        write_week3_report(reservation, _report("source-swap"))

    assert not reservation.final_dir.exists()
    assert not (reservation.parent_dir / ".owned-moved").exists()


def test_publish_postcheck_failure_removes_owned_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="postcheck",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )

    def fail_postcheck(_reservation: object) -> None:
        raise OSError("fixed postcheck failure")

    monkeypatch.setattr(reporting, "_validate_published", fail_postcheck)
    with pytest.raises(OSError, match="fixed postcheck failure"):
        write_week3_report(reservation, _report("postcheck"))

    assert not reservation.final_dir.exists()
    assert not tuple(reservation.parent_dir.glob(".week3-foreign-*"))


def test_writer_revalidates_model_copy_bypass_and_scans_string_values(tmp_path: Path) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="unsafe-copy",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    unsafe = _report("unsafe-copy").model_copy(update={"requested_model": "sk-secret"})

    with pytest.raises(ValueError, match="unsafe metadata"):
        write_week3_report(reservation, unsafe)


def test_recursive_boundary_rejects_sentinel_in_every_serialized_string_field(
    tmp_path: Path,
) -> None:
    payload = _report("all-fields").model_dump(mode="json")
    paths: list[tuple[object, ...]] = []

    def collect(value: object, path: tuple[object, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, (*path, index))
        elif isinstance(value, str):
            paths.append(path)

    def replace(value: object, path: tuple[object, ...]) -> None:
        target = value
        for item in path[:-1]:
            target = target[item]  # type: ignore[index]
        target[path[-1]] = "ROW_SENTINEL_SELECT_SK_ENDPOINT"  # type: ignore[index]

    collect(payload)
    scan = reporting.__dict__["_scan_safe"]
    assert paths
    for path in paths:
        mutated = deepcopy(payload)
        replace(mutated, path)
        with pytest.raises(ValueError, match="unsafe metadata"):
            scan(mutated)

    for forbidden_key in ("sql", "prompt", "question", "raw_rows", "payload", "endpoint"):
        with pytest.raises(ValueError, match="unsafe metadata"):
            scan({forbidden_key: "safe_identifier"})

    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="unsafe-sentinel",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    case = (
        _report("unsafe-sentinel")
        .cases[0]
        .model_copy(update={"observed_missing_fields": ("ROW_SENTINEL",)})
    )
    unsafe = _report("unsafe-sentinel").model_copy(update={"cases": (case,)})

    with pytest.raises(ValueError, match="unsafe metadata"):
        write_week3_report(reservation, unsafe)


def test_reservation_close_retries_only_failed_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="close-retry",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    remove_owned = reporting.__dict__["_remove_owned"]
    remove_owned(reservation)
    reservation._active = False
    original_close = os.close
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if fd == reservation.staging_fd and not failed:
            failed = True
            raise OSError("fixed close failure")
        original_close(fd)

    monkeypatch.setattr(os, "close", fail_once)
    with pytest.raises(OSError, match="fixed close failure"):
        reservation.close()
    assert not reservation.closed
    reservation.close()
    assert reservation.closed


def test_close_failure_does_not_mask_primary_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="primary-error",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original_close = os.close

    def fail_staging_close(fd: int) -> None:
        if fd == reservation.staging_fd:
            raise OSError("close failure")
        original_close(fd)

    def fail_write(_directory_fd: int, _name: str, _contents: str) -> None:
        raise OSError("primary write failure")

    with monkeypatch.context() as context:
        context.setattr(os, "close", fail_staging_close)
        context.setattr(reporting, "_write_text_at", fail_write)
        with pytest.raises(OSError, match="primary write failure"):
            write_week3_report(reservation, _report("primary-error"))

    original_close(reservation.staging_fd)
    reservation._staging_fd_closed = True
    assert reservation.closed
