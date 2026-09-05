"""Atomic, no-replace publication of sanitized Week 3 reports."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from governed_analytics.evals.reporting import _publish_staged_directory, _write_text
from governed_analytics.evals.week3.models import Week3CaseResult, Week3RunReport

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True, slots=True)
class Week3ReportReservation:
    mode: str
    run_id: str
    timestamp: datetime
    final_dir: Path
    staging_dir: Path
    staging_device: int
    staging_inode: int
    _active: bool = field(default=True, init=False, repr=False, compare=False)

    @property
    def active(self) -> bool:
        return self._active


@dataclass(frozen=True, slots=True)
class PublishedWeek3Report:
    report_dir: Path
    report_json: Path
    report_markdown: Path
    cases_dir: Path


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _remove_owned(reservation: Week3ReportReservation) -> None:
    try:
        current = reservation.staging_dir.lstat()
        if (
            current.st_dev != reservation.staging_device
            or current.st_ino != reservation.staging_inode
            or not stat.S_ISDIR(current.st_mode)
        ):
            return
        for child in sorted(reservation.staging_dir.rglob("*"), reverse=True):
            if child.is_symlink():
                return
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        reservation.staging_dir.rmdir()
    except OSError:
        return


def cancel_week3_report_reservation(reservation: Week3ReportReservation) -> None:
    if reservation.active:
        _remove_owned(reservation)
        object.__setattr__(reservation, "_active", False)


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Week 3 report timestamp must be timezone-aware")
    return value.astimezone(UTC)


def reserve_week3_report(
    output_root: str | Path,
    *,
    mode: str,
    run_id: str,
    timestamp: datetime,
) -> Week3ReportReservation:
    normalized = _normalize_timestamp(timestamp)
    if mode not in {"fixture", "live"} or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Week 3 report target metadata is invalid")
    mode_dir = Path(output_root).absolute() / mode
    name = f"{normalized.strftime('%Y%m%dT%H%M%SZ')}-{run_id}"
    final_dir = mode_dir / name
    staging_dir = mode_dir / f".{name}-staging-reservation"
    try:
        mode_dir.mkdir(parents=True, exist_ok=True)
        if mode_dir.is_symlink() or _exists(final_dir):
            raise FileExistsError
        staging_dir.mkdir(mode=0o700)
        metadata = staging_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OSError
    except FileExistsError:
        raise FileExistsError("Week 3 report already exists or is reserved") from None
    except OSError:
        raise OSError("Week 3 report reservation unavailable") from None
    reservation = Week3ReportReservation(
        mode=mode,
        run_id=run_id,
        timestamp=normalized,
        final_dir=final_dir,
        staging_dir=staging_dir,
        staging_device=metadata.st_dev,
        staging_inode=metadata.st_ino,
    )
    try:
        probe_source = staging_dir / f".source-{uuid4().hex}"
        probe_source.write_bytes(b"")
        probe_source.unlink()
        if _exists(final_dir):
            raise FileExistsError
    except BaseException:
        cancel_week3_report_reservation(reservation)
        raise
    return reservation


def _serialise(model: Week3RunReport | Week3CaseResult) -> str:
    return (
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )


def _markdown(report: Week3RunReport) -> str:
    lines = ["# Week 3 agent evaluation", ""]
    if report.mode == "fixture":
        lines.extend(
            ["**Harness validation only; this is not a claim of live model quality.**", ""]
        )
    lines.extend(
        [
            f"- Run ID: {report.run_id}",
            f"- Mode: {report.mode}",
            f"- Executed cases: {report.case_count}",
            f"- Passed cases: {report.passed_count}",
            f"- Known/Heldout: {report.known_count}/{report.heldout_count}",
            f"- Known passed: {report.known_passed_count}",
            f"- Heldout passed: {report.heldout_passed_count}",
            f"- Behavior conformant: {report.behavior_passed}",
            f"- Simple strict passes: {report.simple_strict_passed}",
            f"- Attribution passed: {report.attribution_passed}/{report.attribution_count}",
            "- First/final strict passes: "
            f"{report.first_candidate_strict_passed}/{report.final_candidate_strict_passed}",
            f"- Tool conformant: {report.tool_conformant_count}",
            f"- Evidence conformant: {report.evidence_conformant_count}",
            f"- Budget conformant: {report.budget_conformant_count}",
            f"- Requested model: {report.requested_model}",
            f"- Resolved models: {', '.join(report.resolved_models) or '(none)'}",
            f"- Pricing effective date: {report.pricing_effective_date or '(none)'}",
            f"- Pricing snapshot: {report.pricing_snapshot_sha256 or '(none)'}",
            f"- Executed manifest: {report.executed_manifest_sha256}",
            "",
            "| Case | Cohort | Suite | Passed | First | Final | Behavior | Tool | "
            "Evidence | Budget | Status | Reason |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in report.cases:
        lines.append(
            "| "
            + " | ".join(
                (
                    case.case_id,
                    case.cohort,
                    case.suite,
                    str(case.passed).lower(),
                    ""
                    if case.first_candidate_score is None
                    else str(case.first_candidate_score.strict_pass).lower(),
                    ""
                    if case.final_candidate_score is None
                    else str(case.final_candidate_score.strict_pass).lower(),
                    str(case.behavior_conformant).lower(),
                    str(case.tools_conformant).lower(),
                    ""
                    if case.evidence_conformant is None
                    else str(case.evidence_conformant).lower(),
                    str(case.budget_conformant).lower(),
                    "" if case.observed_final_status is None else case.observed_final_status.value,
                    "" if case.observed_stop_reason is None else case.observed_stop_reason.value,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _validate_reservation(reservation: Week3ReportReservation, report: Week3RunReport) -> None:
    try:
        current = reservation.staging_dir.lstat()
    except OSError:
        raise OSError("Week 3 report reservation unavailable") from None
    if (
        not reservation.active
        or report.mode != reservation.mode
        or report.run_id != reservation.run_id
        or _exists(reservation.final_dir)
        or current.st_dev != reservation.staging_device
        or current.st_ino != reservation.staging_inode
        or not stat.S_ISDIR(current.st_mode)
        or any(reservation.staging_dir.iterdir())
    ):
        raise OSError("Week 3 report reservation unavailable")


def write_week3_report(
    reservation: Week3ReportReservation,
    report: Week3RunReport,
) -> PublishedWeek3Report:
    try:
        report = Week3RunReport.model_validate(
            report.model_dump(exclude_computed_fields=True), strict=True
        )
    except ValidationError as error:
        cancel_week3_report_reservation(reservation)
        raise ValueError("Week 3 report contains unsafe metadata") from error
    try:
        _validate_reservation(reservation, report)
        cases_dir = reservation.staging_dir / "cases"
        cases_dir.mkdir()
        _write_text(reservation.staging_dir / "report.json", _serialise(report))
        _write_text(reservation.staging_dir / "report.md", _markdown(report))
        for case in report.cases:
            _write_text(cases_dir / f"{case.case_id}.json", _serialise(case))
        _publish_staged_directory(reservation.staging_dir, reservation.final_dir)
        published = reservation.final_dir.lstat()
        if (
            _exists(reservation.staging_dir)
            or published.st_dev != reservation.staging_device
            or published.st_ino != reservation.staging_inode
        ):
            raise OSError("atomic Week 3 report publication failed")
    except BaseException:
        cancel_week3_report_reservation(reservation)
        raise
    object.__setattr__(reservation, "_active", False)
    return PublishedWeek3Report(
        report_dir=reservation.final_dir,
        report_json=reservation.final_dir / "report.json",
        report_markdown=reservation.final_dir / "report.md",
        cases_dir=reservation.final_dir / "cases",
    )


__all__ = [
    "PublishedWeek3Report",
    "Week3ReportReservation",
    "cancel_week3_report_reservation",
    "reserve_week3_report",
    "write_week3_report",
]
