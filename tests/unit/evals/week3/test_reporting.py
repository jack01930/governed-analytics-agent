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
from governed_analytics.config import AgentRuntimeSettings
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
    Week3PublicationEvidence,
    cancel_week3_report_reservation,
    reserve_week3_report,
    write_week3_report,
)
from governed_analytics.evals.week3.runner import (
    _fixture_pricing,
    _rebuild_report_from_publication_evidence,
    _safe_failure,
)
from governed_analytics.evals.week3.suites import load_week3_cases


def _report(run_id: str) -> Week3RunReport:
    case = Week3CaseResult(
        case_id="W3K001",
        cohort="known",
        suite="behavior",
        expected_behavior=BehaviorAction.CLARIFY,
        expected_missing_fields=("metric", "time_window"),
        expected_final_status=FinalStatus.CLARIFICATION_REQUIRED,
        expected_stop_reason=StopReason.MISSING_REQUIRED_FIELDS,
        observed_final_status=FinalStatus.CLARIFICATION_REQUIRED,
        observed_stop_reason=StopReason.MISSING_REQUIRED_FIELDS,
        observed_behavior=BehaviorAction.CLARIFY,
        observed_behavior_reason=BehaviorReasonCode.MISSING_METRIC,
        observed_missing_fields=("metric", "time_window"),
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
        natural_refusal=True,
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


def _publication_pair(
    run_id: str,
) -> tuple[Week3RunReport, Week3PublicationEvidence]:
    generated = datetime(2026, 9, 4, tzinfo=UTC)
    case = load_week3_cases()[:1]
    evidence = Week3PublicationEvidence(
        mode="fixture",
        cases=case,
        outcomes=(_safe_failure(case_id=case[0].case_id, reason=StopReason.INTERNAL_ERROR),),
        error_types=(None,),
        expected_results=((),),
        settings=AgentRuntimeSettings(),
        pricing=_fixture_pricing(),
        generated_at_utc=generated,
        run_id=run_id,
    )
    return _rebuild_report_from_publication_evidence(evidence), evidence


def _quarantined_paths(parent: Path) -> tuple[Path, ...]:
    return tuple(parent.glob(".week3-foreign-*"))


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


def _replace_regular_file(directory_fd: int, name: str, contents: bytes = b"foreign") -> None:
    os.unlink(name, dir_fd=directory_fd)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    try:
        os.write(fd, contents)
    finally:
        os.close(fd)


@pytest.mark.parametrize("target", ("report.json", "report.md", "W3K001.json"))
def test_written_regular_file_inode_swap_is_rejected_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id=f"file-swap-{target.split('.')[0]}",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original = reporting._write_text_at
    swapped = False

    def swap_after_write(directory_fd: int, name: str, contents: str) -> object:
        nonlocal swapped
        binding = original(directory_fd, name, contents)
        if name == target and not swapped:
            swapped = True
            _replace_regular_file(directory_fd, name)
        return binding

    monkeypatch.setattr(reporting, "_write_text_at", swap_after_write)
    with pytest.raises(OSError, match="inventory changed"):
        write_week3_report(reservation, _report(reservation.run_id))

    assert not reservation.final_dir.exists()
    quarantines = tuple(reservation.parent_dir.glob(".week3-foreign-*"))
    assert len(quarantines) == 1
    assert any(
        path.read_bytes() == b"foreign"
        for path in quarantines
        if path.is_file()
    )


def test_file_swap_after_publisher_identity_check_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="publisher-file-swap",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original = reporting.__dict__["_native_publish_owned"]

    def swap_then_publish(active_reservation: object) -> None:
        _replace_regular_file(reservation.staging_fd, "report.json")
        original(active_reservation)

    monkeypatch.setattr(reporting, "_native_publish_owned", swap_then_publish)
    with pytest.raises(OSError, match="inventory changed"):
        write_week3_report(reservation, _report("publisher-file-swap"))

    assert not reservation.final_dir.exists()
    assert tuple(reservation.parent_dir.glob(".week3-foreign-*"))


def test_component_swap_during_reservation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "component"
    original_mkdir = os.mkdir
    swapped = False

    def swap_component(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if path == "fixture" and dir_fd is not None and not swapped:
            swapped = True
            output.rename(tmp_path / "owned-component")
            original_mkdir(output, 0o700)
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_component)
    with pytest.raises(OSError):
        reserve_week3_report(
            output,
            mode="fixture",
            run_id="component-swap",
            timestamp=datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert output.is_dir()
    assert not (output / "fixture" / "20260904T000000Z-component-swap").exists()


@pytest.mark.parametrize("entry_type", ("regular", "directory", "symlink"))
@pytest.mark.parametrize("placement", ("root", "cases"))
def test_extra_inventory_is_quarantined_without_deleting_foreign_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_type: str,
    placement: str,
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id=f"extra-{placement}-{entry_type}",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original = reporting.__dict__["_validate_inventory"]
    injected = False

    def inject_extra(
        active_reservation: object,
        cases_fd: int,
        case_names: tuple[str, ...],
        *,
        published: bool = False,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            directory_fd = reservation.staging_fd if placement == "root" else cases_fd
            if entry_type == "regular":
                fd = os.open(
                    "foreign",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.write(fd, b"foreign-regular")
                os.close(fd)
            elif entry_type == "directory":
                os.mkdir("foreign", dir_fd=directory_fd)
                foreign_fd = os.open(
                    "foreign", os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory_fd
                )
                try:
                    fd = os.open(
                        "marker",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=foreign_fd,
                    )
                    os.write(fd, b"foreign-directory")
                    os.close(fd)
                finally:
                    os.close(foreign_fd)
            else:
                os.symlink("foreign-target", "foreign", dir_fd=directory_fd)
        original(active_reservation, cases_fd, case_names, published=published)

    monkeypatch.setattr(reporting, "_validate_inventory", inject_extra)
    with pytest.raises(OSError, match="inventory changed"):
        write_week3_report(reservation, _report(reservation.run_id))

    assert not reservation.final_dir.exists()
    assert not reservation.staging_dir.exists()
    quarantines = _quarantined_paths(reservation.parent_dir)
    assert len(quarantines) == 1
    if entry_type == "regular":
        assert quarantines[0].read_bytes() == b"foreign-regular"
    elif entry_type == "directory":
        assert (quarantines[0] / "marker").read_bytes() == b"foreign-directory"
    else:
        assert quarantines[0].is_symlink()
        assert os.readlink(quarantines[0]) == "foreign-target"


def test_replaced_cases_directory_is_quarantined_and_foreign_content_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="cases-replacement",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original = reporting.__dict__["_validate_inventory"]
    injected = False

    def replace_cases(
        active_reservation: object,
        cases_fd: int,
        case_names: tuple[str, ...],
        *,
        published: bool = False,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.rename(
                "cases",
                "owned-cases",
                src_dir_fd=reservation.staging_fd,
                dst_dir_fd=reservation.staging_fd,
            )
            os.mkdir("cases", dir_fd=reservation.staging_fd)
            foreign_fd = os.open(
                "cases",
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=reservation.staging_fd,
            )
            try:
                marker_fd = os.open(
                    "marker",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=foreign_fd,
                )
                try:
                    os.write(marker_fd, b"foreign-cases")
                finally:
                    os.close(marker_fd)
            finally:
                os.close(foreign_fd)
        original(active_reservation, cases_fd, case_names, published=published)

    monkeypatch.setattr(reporting, "_validate_inventory", replace_cases)
    with pytest.raises(OSError, match="inventory changed"):
        write_week3_report(reservation, _report(reservation.run_id))

    assert not reservation.final_dir.exists()
    assert not reservation.staging_dir.exists()
    quarantines = _quarantined_paths(reservation.parent_dir)
    assert len(quarantines) == 1
    assert (quarantines[0] / "marker").read_bytes() == b"foreign-cases"


@pytest.mark.parametrize("phase", ("before_publish", "after_publish"))
@pytest.mark.parametrize("target", ("report.json", "report.md", "W3K001.json"))
def test_same_inode_content_tamper_is_quarantined_before_and_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    target: str,
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id=f"tamper-{phase}-{target.split('.')[0]}",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original = reporting.__dict__["_validate_inventory"]
    tampered = False

    def tamper(
        active_reservation: object,
        cases_fd: int,
        case_names: tuple[str, ...],
        *,
        published: bool = False,
    ) -> None:
        nonlocal tampered
        should_tamper = (phase == "before_publish" and not published) or (
            phase == "after_publish" and published
        )
        if should_tamper and not tampered:
            tampered = True
            binding = next(item for item in reservation._file_bindings if item.name == target)
            os.pwrite(binding.fd, b"X", 0)
            os.fsync(binding.fd)
        original(active_reservation, cases_fd, case_names, published=published)

    monkeypatch.setattr(reporting, "_validate_inventory", tamper)
    with pytest.raises(OSError, match="inventory changed"):
        write_week3_report(reservation, _report(reservation.run_id))

    assert not reservation.final_dir.exists()
    assert not reservation.staging_dir.exists()
    quarantines = _quarantined_paths(reservation.parent_dir)
    assert len(quarantines) == 1
    assert quarantines[0].is_file()
    assert quarantines[0].read_bytes().startswith(b"X")


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


def test_writer_rebuilds_report_from_independent_publication_evidence(tmp_path: Path) -> None:
    report, evidence = _publication_pair("independent-evidence")
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="independent-evidence",
        timestamp=evidence.generated_at_utc,
    )
    forged_case = report.cases[0].model_copy(
        update={"error_type": "forged_consistent_aggregate"}
    )
    forged = report.model_copy(update={"cases": (forged_case,)})

    with pytest.raises(ValueError, match="unsafe metadata"):
        write_week3_report(
            reservation,
            forged,
            publication_evidence=evidence,
        )

    assert not reservation.final_dir.exists()


def test_writer_rejects_publication_evidence_with_modified_frozen_case(tmp_path: Path) -> None:
    report, evidence = _publication_pair("forged-evidence")
    forged_case = evidence.cases[0].model_copy(update={"question": "forged but private"})
    forged_evidence = Week3PublicationEvidence(
        mode=evidence.mode,
        cases=(forged_case,),
        outcomes=evidence.outcomes,
        error_types=evidence.error_types,
        expected_results=evidence.expected_results,
        settings=evidence.settings,
        pricing=evidence.pricing,
        generated_at_utc=evidence.generated_at_utc,
        run_id=evidence.run_id,
    )
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id=evidence.run_id,
        timestamp=evidence.generated_at_utc,
    )

    with pytest.raises(ValueError, match="unsafe metadata"):
        write_week3_report(
            reservation,
            report,
            publication_evidence=forged_evidence,
        )

    assert not reservation.final_dir.exists()


def test_recursive_boundary_rejects_sensitive_shapes_in_every_serialized_string_field(
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
        target[path[-1]] = "SELECT secret_value FROM protected_table"  # type: ignore[index]

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
        run_id="unsafe-sensitive",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    case = (
        _report("unsafe-sensitive")
        .cases[0]
        .model_copy(update={"observed_missing_fields": ("ROW_SENTINEL",)})
    )
    unsafe = _report("unsafe-sensitive").model_copy(update={"cases": (case,)})

    with pytest.raises(ValueError, match="unsafe metadata"):
        write_week3_report(reservation, unsafe)


def test_recursive_boundary_allows_plain_sentinel_and_sql_keywords_in_prose() -> None:
    scan = reporting.__dict__["_scan_safe"]

    scan(
        {
            "requested_model": "sentinel-llm",
            "error_type": "selected_from_cache",
            "detail": "please select a cached model",
            "operation": "drop shipping is selected from cache",
        }
    )


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT 1",
        "VALUES (1)",
        "GRANT SELECT ON metrics TO analyst",
        "COPY metrics TO STDOUT",
        "INSERT INTO metrics VALUES (1)",
        "UPDATE metrics SET value = 1",
        "DELETE FROM metrics",
        "CREATE TABLE metrics (value integer)",
        "ALTER TABLE metrics ADD COLUMN label text",
        "DROP TABLE metrics",
        "WITH metric AS (SELECT 1) SELECT * FROM metric",
    ),
)
def test_recursive_boundary_rejects_complete_sql_statements(sql: str) -> None:
    scan = reporting.__dict__["_scan_safe"]

    with pytest.raises(ValueError, match="unsafe metadata"):
        scan({"detail": sql})


def test_reservation_close_detaches_failed_descriptor_without_retry(
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
    assert reservation.closed
    assert reservation.close_unknown
    reservation.close()
    original_close(reservation.staging_fd)


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
    assert reservation.closed


@pytest.mark.parametrize(
    "target",
    ("report.json", "W3K001.json", "cases", "staging", "parent"),
)
@pytest.mark.parametrize("failure_mode", ("before_syscall", "after_syscall_reuse"))
def test_postpublish_close_failure_detaches_each_descriptor_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    failure_mode: str,
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id=f"postpublish-close-{target.split('.')[0]}-{failure_mode}",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original_close = os.close
    sentinel_read, sentinel_write = os.pipe()
    calls = 0

    def target_fd() -> int | None:
        if target == "cases":
            return reservation._cases_fd
        if target == "staging":
            return reservation.staging_fd
        if target == "parent":
            return reservation.parent_fd
        return next(
            (binding.fd for binding in reservation._file_bindings if binding.name == target),
            None,
        )

    def fail_target(fd: int) -> None:
        nonlocal calls
        if target_fd() == fd:
            calls += 1
            if failure_mode == "after_syscall_reuse":
                original_close(fd)
                os.dup2(sentinel_read, fd)
            raise OSError("fixed close failure")
        original_close(fd)

    with monkeypatch.context() as context:
        context.setattr(os, "close", fail_target)
        published = write_week3_report(reservation, _report(reservation.run_id))

    assert published.report_json.is_file()
    assert calls == 1
    assert reservation.closed
    assert reservation.close_unknown
    detached_fd = target_fd()
    assert detached_fd is not None
    os.fstat(detached_fd)
    original_close(detached_fd)
    original_close(sentinel_read)
    original_close(sentinel_write)


def test_close_attempts_all_remaining_descriptors_after_multiple_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="multiple-close-errors",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    original_close = os.close
    failed_fds: list[int] = []

    def fail_staging_and_parent(fd: int) -> None:
        if fd in {reservation.staging_fd, reservation.parent_fd}:
            failed_fds.append(fd)
            raise OSError("fixed persistent close failure")
        original_close(fd)

    with monkeypatch.context() as context:
        context.setattr(os, "close", fail_staging_and_parent)
        published = write_week3_report(reservation, _report(reservation.run_id))

    assert published.report_json.is_file()
    assert failed_fds == [reservation.staging_fd, reservation.parent_fd]
    assert reservation.closed
    assert reservation.close_unknown
    original_close(reservation.staging_fd)
    original_close(reservation.parent_fd)


@pytest.mark.parametrize(
    "walker_name", ("_open_directory_tree", "_open_or_create_directory_tree")
)
def test_directory_walker_keeps_child_owned_when_parent_close_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, walker_name: str
) -> None:
    original_open = os.open
    original_close = os.close
    sentinel_read, sentinel_write = os.pipe()
    opened: list[int] = []
    close_calls = 0

    def track_open(*args: object, **kwargs: object) -> int:
        fd = original_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    def close_parent_then_reuse(fd: int) -> None:
        nonlocal close_calls
        if opened and fd == opened[0]:
            close_calls += 1
            original_close(fd)
            os.dup2(sentinel_read, fd)
            raise OSError("fixed parent close failure")
        original_close(fd)

    target = tmp_path if walker_name == "_open_directory_tree" else tmp_path / "created"
    with monkeypatch.context() as context:
        context.setattr(os, "open", track_open)
        context.setattr(os, "close", close_parent_then_reuse)
        with pytest.raises(OSError, match="fixed parent close failure"):
            reporting.__dict__[walker_name](target)

    assert close_calls == 1
    assert len(opened) == 2
    os.fstat(opened[0])
    with pytest.raises(OSError):
        os.fstat(opened[1])
    original_close(opened[0])
    original_close(sentinel_read)
    original_close(sentinel_write)
