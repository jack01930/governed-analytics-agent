"""Descriptor-owned, atomic publication of sanitized Week 3 reports."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from governed_analytics.evals.week3.models import Week3CaseResult, Week3RunReport

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SQL_TEXT = re.compile(r"(?i)\b(select|insert|update|delete|alter|drop|create|with)\s+")
_URL = re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql)://")
_CREDENTIAL = re.compile(r"(?i)(?:^|[^A-Za-z0-9])(?:sk-|pk-|bearer\s+)")
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "deadline_monotonic",
        "endpoint",
        "oracle_query_id",
        "params",
        "password",
        "payload",
        "prompt",
        "question",
        "raw_rows",
        "secret",
        "sql",
    }
)


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int


@dataclass(slots=True)
class _FileBinding:
    directory_fd: int
    name: str
    fd: int
    identity: _Identity
    size: int
    digest: str
    closed: bool = False


@dataclass(slots=True)
class Week3ReportReservation:
    mode: str
    run_id: str
    timestamp: datetime
    final_dir: Path
    staging_dir: Path
    parent_dir: Path
    staging_name: str
    final_name: str
    staging_identity: _Identity
    parent_identity: _Identity
    staging_fd: int
    parent_fd: int
    _active: bool = True
    _staging_fd_closed: bool = False
    _parent_fd_closed: bool = False
    _cases_fd: int | None = None
    _cases_identity: _Identity | None = None
    _cases_fd_closed: bool = True
    _case_names: tuple[str, ...] = ()
    _file_bindings: list[_FileBinding] = field(default_factory=list)
    _foreign_content: bool = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def closed(self) -> bool:
        return (
            self._staging_fd_closed
            and self._parent_fd_closed
            and self._cases_fd_closed
            and all(item.closed for item in self._file_bindings)
        )

    def close(self) -> None:
        first: OSError | None = None
        for binding in self._file_bindings:
            if binding.closed:
                continue
            try:
                os.close(binding.fd)
            except OSError as error:
                if first is None:
                    first = error
            else:
                binding.closed = True
        if not self._cases_fd_closed and self._cases_fd is not None:
            try:
                os.close(self._cases_fd)
            except OSError as error:
                if first is None:
                    first = error
            else:
                self._cases_fd_closed = True
        if not self._staging_fd_closed:
            try:
                os.close(self.staging_fd)
            except OSError as error:
                first = error
            else:
                self._staging_fd_closed = True
        if not self._parent_fd_closed:
            try:
                os.close(self.parent_fd)
            except OSError as error:
                if first is None:
                    first = error
            else:
                self._parent_fd_closed = True
        if first is not None:
            raise first


@dataclass(frozen=True, slots=True)
class PublishedWeek3Report:
    report_dir: Path
    report_json: Path
    report_markdown: Path
    cases_dir: Path


def _identity(metadata: os.stat_result) -> _Identity:
    return _Identity(metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _directory_handle_identity(fd: int) -> _Identity:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("Week 3 report reservation unavailable")
    return _identity(metadata)


def _directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("secure Week 3 report handles are unavailable")
    return int(
        os.O_RDONLY | no_follow | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    )


def _open_or_create_directory_tree(path: Path) -> int:
    absolute = path.absolute()
    fd = os.open(absolute.anchor, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        raise


def _open_directory_tree(path: Path) -> int:
    absolute = path.absolute()
    fd = os.open(absolute.anchor, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        raise


def _path_is_bound_to(path: Path, identity: _Identity) -> bool:
    fd = _open_directory_tree(path)
    try:
        return _directory_handle_identity(fd) == identity
    finally:
        os.close(fd)


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
    root = Path(output_root).absolute()
    root_fd = _open_or_create_directory_tree(root)
    mode_dir = root / mode
    name = f"{normalized.strftime('%Y%m%dT%H%M%SZ')}-{run_id}"
    staging_name = f".{name}-staging-reservation"
    parent_fd: int | None = None
    staging_fd: int | None = None
    created = False
    try:
        with suppress(FileExistsError):
            os.mkdir(mode, mode=0o700, dir_fd=root_fd)
        parent_fd = os.open(mode, _directory_flags(), dir_fd=root_fd)
        os.close(root_fd)
        root_fd = -1
        parent_identity = _directory_handle_identity(parent_fd)
        if not _path_is_bound_to(mode_dir, parent_identity):
            raise OSError("Week 3 report reservation unavailable")
        for candidate in (name, staging_name):
            try:
                os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("Week 3 report already exists or is reserved")
        os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
        created = True
        staging_fd = os.open(staging_name, _directory_flags(), dir_fd=parent_fd)
        staging_identity = _directory_handle_identity(staging_fd)
        if (
            _identity(os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False))
            != staging_identity
            or stat.S_IMODE(staging_identity.mode) != 0o700
        ):
            raise OSError("Week 3 report reservation unavailable")
        return Week3ReportReservation(
            mode=mode,
            run_id=run_id,
            timestamp=normalized,
            final_dir=mode_dir / name,
            staging_dir=mode_dir / staging_name,
            parent_dir=mode_dir,
            staging_name=staging_name,
            final_name=name,
            staging_identity=staging_identity,
            parent_identity=parent_identity,
            staging_fd=staging_fd,
            parent_fd=parent_fd,
        )
    except BaseException:
        if staging_fd is not None:
            with suppress(OSError):
                os.close(staging_fd)
        if created:
            with suppress(OSError):
                os.rmdir(staging_name, dir_fd=parent_fd)
        if parent_fd is not None:
            with suppress(OSError):
                os.close(parent_fd)
        if root_fd >= 0:
            with suppress(OSError):
                os.close(root_fd)
        raise


def _validate_owned(reservation: Week3ReportReservation, *, require_path: bool = True) -> None:
    if not reservation.active or reservation.closed:
        raise OSError("Week 3 report reservation unavailable")
    if (
        _directory_handle_identity(reservation.parent_fd) != reservation.parent_identity
        or not _path_is_bound_to(reservation.parent_dir, reservation.parent_identity)
        or _directory_handle_identity(reservation.staging_fd) != reservation.staging_identity
        or (
            require_path
            and _identity(
                os.stat(
                    reservation.staging_name,
                    dir_fd=reservation.parent_fd,
                    follow_symlinks=False,
                )
            )
            != reservation.staging_identity
        )
    ):
        raise OSError("Week 3 report reservation unavailable")


def _find_owned_name(reservation: Week3ReportReservation) -> str | None:
    if reservation._parent_fd_closed:
        return None
    for name in os.listdir(reservation.parent_fd):
        try:
            metadata = os.stat(name, dir_fd=reservation.parent_fd, follow_symlinks=False)
        except OSError:
            continue
        if _identity(metadata) == reservation.staging_identity:
            return name
    return None


def _empty_directory(fd: int) -> None:
    for name in os.listdir(fd):
        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=fd,
            )
            try:
                _empty_directory(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def _remove_owned(reservation: Week3ReportReservation) -> None:
    name = _find_owned_name(reservation)
    if name is None or reservation._staging_fd_closed:
        return
    if _directory_handle_identity(reservation.staging_fd) != reservation.staging_identity:
        return
    _empty_directory(reservation.staging_fd)
    os.rmdir(name, dir_fd=reservation.parent_fd)


def cancel_week3_report_reservation(reservation: Week3ReportReservation) -> None:
    if reservation.active:
        with suppress(OSError):
            _remove_owned(reservation)
        reservation._active = False
    with suppress(OSError):
        reservation.close()


def _scan_safe(value: object, *, key: str | None = None) -> None:
    if key is not None:
        snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", snake).strip("_").casefold()
        if normalized in _FORBIDDEN_KEY_TOKENS:
            raise ValueError("Week 3 report contains unsafe metadata")
    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise ValueError("Week 3 report contains unsafe metadata")
            _scan_safe(child, key=child_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _scan_safe(child)
    elif isinstance(value, str) and (
        _SQL_TEXT.search(value)
        or _URL.search(value)
        or _CREDENTIAL.search(value)
    ):
        raise ValueError("Week 3 report contains unsafe metadata")


def _dump(model: Week3RunReport | Week3CaseResult) -> dict[str, object]:
    value = model.model_dump(mode="json")
    _scan_safe(value)
    return value


def _serialise(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _metric_line(label: str, numerator: int, denominator: int) -> str:
    return f"- {label}: {numerator}/{denominator if denominator else 'N/A'}"


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
            f"- Report scope: {report.report_scope}",
            _metric_line("Overall", report.passed_count, report.case_count),
            _metric_line("Known", report.known_metric.numerator, report.known_metric.denominator),
            _metric_line(
                "Heldout", report.heldout_metric.numerator, report.heldout_metric.denominator
            ),
            _metric_line(
                "Behavior", report.behavior_metric.numerator, report.behavior_metric.denominator
            ),
            _metric_line(
                "Simple", report.simple_metric.numerator, report.simple_metric.denominator
            ),
            _metric_line(
                "Attribution",
                report.attribution_metric.numerator,
                report.attribution_metric.denominator,
            ),
            _metric_line("Tool", report.tool_metric.numerator, report.tool_metric.denominator),
            _metric_line(
                "Evidence", report.evidence_metric.numerator, report.evidence_metric.denominator
            ),
            _metric_line(
                "Budget", report.budget_metric.numerator, report.budget_metric.denominator
            ),
            _metric_line(
                "Repair success", report.repair_metric.numerator, report.repair_metric.denominator
            ),
            _metric_line(
                "Valid execute",
                report.valid_execute_metric.numerator,
                report.valid_execute_metric.denominator,
            ),
            _metric_line(
                "Natural refusal",
                report.natural_refusal_metric.numerator,
                report.natural_refusal_metric.denominator,
            ),
        ]
    )
    for name, metric in report.candidate_metrics.items():
        lines.append(_metric_line(name, metric.numerator, metric.denominator))
    lines.extend(
        [
            f"- Requested model: {report.requested_model}",
            f"- Resolved models: {', '.join(report.resolved_models) or '(none)'}",
            f"- Executed manifest: {report.executed_manifest_sha256}",
            "",
            "| Case | Cohort | Suite | Passed | Status | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
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
                    "" if case.observed_final_status is None else case.observed_final_status.value,
                    "" if case.observed_stop_reason is None else case.observed_stop_reason.value,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _write_text_at(directory_fd: int, name: str, contents: str) -> _FileBinding:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        payload = contents.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Week 3 report inventory changed")
        return _FileBinding(
            directory_fd=directory_fd,
            name=name,
            fd=fd,
            identity=_identity(metadata),
            size=metadata.st_size,
            digest=_digest_fd(fd),
        )
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        raise


def _validate_file_binding(binding: _FileBinding) -> None:
    if binding.closed:
        raise OSError("Week 3 report inventory changed")
    handle_metadata = os.fstat(binding.fd)
    path_metadata = os.stat(binding.name, dir_fd=binding.directory_fd, follow_symlinks=False)
    if (
        _identity(handle_metadata) != binding.identity
        or _identity(path_metadata) != binding.identity
        or not stat.S_ISREG(path_metadata.st_mode)
        or handle_metadata.st_size != binding.size
        or path_metadata.st_size != binding.size
        or _digest_fd(binding.fd) != binding.digest
    ):
        raise OSError("Week 3 report inventory changed")


def _validate_file_bindings(reservation: Week3ReportReservation) -> None:
    try:
        for binding in reservation._file_bindings:
            _validate_file_binding(binding)
    except OSError:
        reservation._foreign_content = True
        raise


def _validate_inventory(
    reservation: Week3ReportReservation,
    cases_fd: int,
    case_names: tuple[str, ...],
    *,
    published: bool = False,
) -> None:
    _validate_owned(reservation, require_path=not published)
    if set(os.listdir(reservation.staging_fd)) != {"report.json", "report.md", "cases"}:
        raise OSError("Week 3 report inventory changed")
    if set(os.listdir(cases_fd)) != set(case_names):
        raise OSError("Week 3 report inventory changed")
    for name in ("report.json", "report.md"):
        metadata = os.stat(name, dir_fd=reservation.staging_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Week 3 report inventory changed")
    cases_metadata = os.stat("cases", dir_fd=reservation.staging_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(cases_metadata.st_mode)
        or reservation._cases_identity is None
        or _identity(cases_metadata) != reservation._cases_identity
        or _directory_handle_identity(cases_fd) != reservation._cases_identity
    ):
        raise OSError("Week 3 report inventory changed")
    for name in case_names:
        metadata = os.stat(name, dir_fd=cases_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Week 3 report inventory changed")
    _validate_file_bindings(reservation)


def _quarantine_final(reservation: Week3ReportReservation) -> None:
    for _attempt in range(16):
        quarantine = f".week3-foreign-{uuid4().hex}"
        try:
            _native_rename_at_no_replace(reservation.parent_fd, reservation.final_name, quarantine)
        except FileExistsError:
            continue
        except FileNotFoundError:
            return
        return
    raise OSError("atomic Week 3 report publication failed")


def _raise_publish_error(error_number: int) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError("Week 3 report already exists or is reserved") from None
    raise OSError("atomic Week 3 report publication failed") from None


def _native_rename_at_no_replace(parent_fd: int, source: str, destination: str) -> None:
    system = platform.system()
    if system == "Darwin":
        try:
            renameatx_np = ctypes.CDLL(None, use_errno=True).renameatx_np
        except AttributeError:
            raise OSError("atomic Week 3 report publication unavailable") from None
        renameatx_np.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameatx_np.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameatx_np(
            parent_fd,
            os.fsencode(source),
            parent_fd,
            os.fsencode(destination),
            0x00000004,
        )
    elif system == "Linux":
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError:
            raise OSError("atomic Week 3 report publication unavailable") from None
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(
            parent_fd,
            os.fsencode(source),
            parent_fd,
            os.fsencode(destination),
            1,
        )
    else:
        raise OSError("atomic Week 3 report publication unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ENOENT:
            raise FileNotFoundError from None
        _raise_publish_error(error_number)


def _native_publish_owned(reservation: Week3ReportReservation) -> None:
    _native_rename_at_no_replace(
        reservation.parent_fd, reservation.staging_name, reservation.final_name
    )


def _publish_owned(reservation: Week3ReportReservation) -> None:
    _validate_owned(reservation)
    _native_publish_owned(reservation)


def _validate_published(reservation: Week3ReportReservation) -> None:
    _validate_owned(reservation, require_path=False)
    try:
        final_identity = _identity(
            os.stat(reservation.final_name, dir_fd=reservation.parent_fd, follow_symlinks=False)
        )
    except OSError:
        raise OSError("atomic Week 3 report publication failed") from None
    if final_identity != reservation.staging_identity:
        raise OSError("atomic Week 3 report publication failed")
    if reservation._cases_fd is None or reservation._cases_fd_closed:
        raise OSError("atomic Week 3 report publication failed")
    _validate_inventory(
        reservation, reservation._cases_fd, reservation._case_names, published=True
    )


def _close_reservation_best_effort(reservation: Week3ReportReservation) -> None:
    for _attempt in range(4):
        if reservation.closed:
            return
        with suppress(OSError):
            reservation.close()


def _quarantine_owned_with_foreign_content(reservation: Week3ReportReservation) -> None:
    owned_name = _find_owned_name(reservation)
    if owned_name is None:
        return
    for _attempt in range(16):
        quarantine = f".week3-foreign-{uuid4().hex}"
        try:
            _native_rename_at_no_replace(reservation.parent_fd, owned_name, quarantine)
        except FileExistsError:
            continue
        except FileNotFoundError:
            return
        return
    raise OSError("atomic Week 3 report publication failed")


def write_week3_report(
    reservation: Week3ReportReservation,
    report: Week3RunReport,
) -> PublishedWeek3Report:
    try:
        report = Week3RunReport.model_validate(
            report.model_dump(exclude_computed_fields=True), strict=True
        )
        report_value = _dump(report)
        case_values = tuple(_dump(case) for case in report.cases)
    except (ValidationError, ValueError) as error:
        cancel_week3_report_reservation(reservation)
        raise ValueError("Week 3 report contains unsafe metadata") from error
    published = False
    primary: BaseException | None = None
    try:
        _validate_owned(reservation)
        if report.mode != reservation.mode or report.run_id != reservation.run_id:
            raise OSError("Week 3 report reservation unavailable")
        os.mkdir("cases", mode=0o700, dir_fd=reservation.staging_fd)
        cases_fd = os.open(
            "cases",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=reservation.staging_fd,
        )
        reservation._cases_fd = cases_fd
        reservation._cases_fd_closed = False
        reservation._cases_identity = _directory_handle_identity(cases_fd)
        reservation._file_bindings.append(
            _write_text_at(reservation.staging_fd, "report.json", _serialise(report_value))
        )
        reservation._file_bindings.append(
            _write_text_at(reservation.staging_fd, "report.md", _markdown(report))
        )
        case_names = tuple(f"{case.case_id}.json" for case in report.cases)
        reservation._case_names = case_names
        for name, value in zip(case_names, case_values, strict=True):
            reservation._file_bindings.append(_write_text_at(cases_fd, name, _serialise(value)))
        os.fsync(cases_fd)
        os.fsync(reservation.staging_fd)
        _validate_inventory(reservation, cases_fd, case_names)
        _publish_owned(reservation)
        published = True
        try:
            _validate_published(reservation)
        except BaseException:
            _quarantine_final(reservation)
            published = False
            raise
        os.fsync(reservation.parent_fd)
    except BaseException as error:
        primary = error
        raise
    finally:
        if primary is not None:
            if reservation._foreign_content:
                with suppress(OSError):
                    _quarantine_owned_with_foreign_content(reservation)
            else:
                with suppress(OSError):
                    _remove_owned(reservation)
            reservation._active = False
            _close_reservation_best_effort(reservation)
    if not published:
        raise OSError("atomic Week 3 report publication failed")
    reservation._active = False
    _close_reservation_best_effort(reservation)
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
