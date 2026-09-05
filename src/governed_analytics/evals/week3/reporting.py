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
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import sqlglot
from sqlglot import exp

from governed_analytics.evals.week3.models import Week3CaseResult, Week3RunReport

if TYPE_CHECKING:
    from governed_analytics.agent.contracts import AgentRunResult
    from governed_analytics.config import AgentRuntimeSettings
    from governed_analytics.evals.models import QueryResult as EvalQueryResult
    from governed_analytics.evals.week3.models import Week3EvaluationCase
    from governed_analytics.pricing import ModelPricing

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_URL = re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql)://")
_CREDENTIAL = re.compile(r"(?i)(?:^|[^A-Za-z0-9])(?:sk-|pk-|bearer\s+)")
_SQL_STRUCTURAL_STATEMENT = re.compile(
    r"(?isx)^\s*(?:"
    r"values\s*\(|"
    r"grant\s+(?:all(?:\s+privileges)?|select|insert|update|delete|truncate|"
    r"references|trigger|usage|execute|connect|create|temporary|temp)\b.*\bon\b.*\bto\b|"
    r"revoke\s+(?:all(?:\s+privileges)?|select|insert|update|delete|truncate|"
    r"references|trigger|usage|execute|connect|create|temporary|temp)\b.*\bon\b.*\bfrom\b|"
    r"copy\s+(?:\([^)]*\)|[A-Za-z_][A-Za-z0-9_$]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_$]*)*)(?:\s*\([^)]*\))?\s+(?:to|from)\b|"
    r"insert\s+into\b|update\s+[^\s;]+\s+set\b|delete\s+from\b|merge\s+into\b|"
    r"create\s+(?:or\s+replace\s+)?(?:materialized\s+)?"
    r"(?:table|view|index|schema|database|function|procedure|type|role)\b|"
    r"alter\s+(?:materialized\s+)?"
    r"(?:table|view|index|schema|database|function|procedure|type|role)\b|"
    r"drop\s+(?:materialized\s+)?"
    r"(?:table|view|index|schema|database|function|procedure|type|role)\b|"
    r"truncate(?:\s+table)?\s+(?:only\s+)?[A-Za-z_][A-Za-z0-9_$]*\b|"
    r"vacuum(?:\s*\([^)]*\))?(?:\s|;|$)|"
    r"call\s+[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*\s*\(|"
    r"begin(?:\s+(?:work|transaction))?\s*;?\s*$|"
    r"commit(?:\s+(?:work|transaction))?\s*;?\s*$|"
    r"rollback(?:\s+(?:work|transaction))?\s*;?\s*$|"
    r"set\s+(?:(?:local|session)\s+)?"
    r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*\s*(?:=|to\b)|"
    r"analyze(?:\s*\([^)]*\))?(?:\s|;|$)"
    r")"
)
_SQL_QUERY_CANDIDATE = re.compile(
    r"(?is)^\s*(?:select\s+|with\s+(?:recursive\s+)?"
    r"[A-Za-z_][A-Za-z0-9_$]*\s+as\s*\()"
)
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
    _close_unknown: bool = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def close_unknown(self) -> bool:
        return self._close_unknown

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
            binding.closed = True
            try:
                os.close(binding.fd)
            except OSError as error:
                self._close_unknown = True
                if first is None:
                    first = error
        if not self._cases_fd_closed and self._cases_fd is not None:
            self._cases_fd_closed = True
            try:
                os.close(self._cases_fd)
            except OSError as error:
                self._close_unknown = True
                if first is None:
                    first = error
        if not self._staging_fd_closed:
            self._staging_fd_closed = True
            try:
                os.close(self.staging_fd)
            except OSError as error:
                self._close_unknown = True
                if first is None:
                    first = error
        if not self._parent_fd_closed:
            self._parent_fd_closed = True
            try:
                os.close(self.parent_fd)
            except OSError as error:
                self._close_unknown = True
                if first is None:
                    first = error
        if first is not None:
            raise first


@dataclass(frozen=True, slots=True)
class PublishedWeek3Report:
    report_dir: Path
    report_json: Path
    report_markdown: Path
    cases_dir: Path


@dataclass(frozen=True, slots=True, repr=False)
class Week3PublicationEvidence:
    mode: Literal["fixture", "live"]
    canonical_cases: tuple[Week3EvaluationCase, ...]
    cases: tuple[Week3EvaluationCase, ...]
    outcomes: tuple[AgentRunResult, ...]
    error_types: tuple[str | None, ...]
    expected_results: tuple[tuple[EvalQueryResult, ...], ...]
    settings: AgentRuntimeSettings
    pricing: ModelPricing
    generated_at_utc: datetime
    run_id: str
    overall_manifest_sha256: str
    known_cohort_sha256: str
    heldout_cohort_sha256: str


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
            old_fd = fd
            fd = -1
            try:
                os.close(old_fd)
            except BaseException:
                with suppress(OSError):
                    os.close(child)
                raise
            fd = child
        return fd
    except BaseException:
        if fd >= 0:
            with suppress(OSError):
                os.close(fd)
        raise


def _open_directory_tree(path: Path) -> int:
    absolute = path.absolute()
    fd = os.open(absolute.anchor, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _directory_flags(), dir_fd=fd)
            old_fd = fd
            fd = -1
            try:
                os.close(old_fd)
            except BaseException:
                with suppress(OSError):
                    os.close(child)
                raise
            fd = child
        return fd
    except BaseException:
        if fd >= 0:
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
        detached_root_fd = root_fd
        root_fd = -1
        os.close(detached_root_fd)
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


def _open_regular_at(directory_fd: int, name: str) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("secure Week 3 report handles are unavailable")
    return os.open(
        name,
        os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=directory_fd,
    )


def _validate_opened_binding(binding: _FileBinding, verification_fd: int) -> None:
    held_before = os.fstat(binding.fd)
    opened_before = os.fstat(verification_fd)
    if (
        _identity(held_before) != binding.identity
        or _identity(opened_before) != binding.identity
        or not stat.S_ISREG(held_before.st_mode)
        or not stat.S_ISREG(opened_before.st_mode)
        or held_before.st_size != binding.size
        or opened_before.st_size != binding.size
    ):
        raise OSError("Week 3 report inventory changed")
    opened_digest = _digest_fd(verification_fd)
    held_digest = _digest_fd(binding.fd)
    held_after = os.fstat(binding.fd)
    opened_after = os.fstat(verification_fd)
    entry_after = os.stat(
        binding.name,
        dir_fd=binding.directory_fd,
        follow_symlinks=False,
    )
    if (
        opened_digest != binding.digest
        or held_digest != binding.digest
        or _identity(held_after) != binding.identity
        or _identity(opened_after) != binding.identity
        or _identity(entry_after) != binding.identity
        or not stat.S_ISREG(entry_after.st_mode)
        or held_after.st_size != binding.size
        or opened_after.st_size != binding.size
        or entry_after.st_size != binding.size
    ):
        raise OSError("Week 3 report inventory changed")


def _unlink_bound_file(binding: _FileBinding) -> None:
    verification_fd = _open_regular_at(binding.directory_fd, binding.name)
    try:
        _validate_opened_binding(binding, verification_fd)
        final_entry = os.stat(
            binding.name,
            dir_fd=binding.directory_fd,
            follow_symlinks=False,
        )
        if _identity(final_entry) != binding.identity or final_entry.st_size != binding.size:
            raise OSError("Week 3 report inventory changed")
        os.unlink(binding.name, dir_fd=binding.directory_fd)
    finally:
        os.close(verification_fd)


def _rmdir_bound_directory(
    directory_fd: int,
    name: str,
    *,
    identity: _Identity,
    held_fd: int,
) -> bool:
    try:
        verification_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
    except OSError:
        return False
    try:
        if (
            _directory_handle_identity(held_fd) != identity
            or _directory_handle_identity(verification_fd) != identity
            or os.listdir(held_fd)
            or os.listdir(verification_fd)
        ):
            return False
        entry_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(entry_after) != identity or not stat.S_ISDIR(entry_after.st_mode):
            return False
        os.rmdir(name, dir_fd=directory_fd)
        return True
    except OSError:
        return False
    finally:
        os.close(verification_fd)


def _quarantine_entry(reservation: Week3ReportReservation, directory_fd: int, name: str) -> None:
    for _attempt in range(16):
        quarantine = f".week3-foreign-{uuid4().hex}"
        try:
            _native_rename_between_no_replace(
                directory_fd,
                name,
                reservation.parent_fd,
                quarantine,
            )
        except FileExistsError:
            continue
        except FileNotFoundError:
            return
        return
    raise OSError("atomic Week 3 report publication failed")


def _quarantine_owned_root(reservation: Week3ReportReservation) -> None:
    for _attempt in range(16):
        owned_name = _find_owned_name(reservation)
        if owned_name is None:
            return
        try:
            metadata = os.stat(
                owned_name,
                dir_fd=reservation.parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            return
        if (
            _identity(metadata) != reservation.staging_identity
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            return
        quarantine = f".week3-foreign-{uuid4().hex}"
        try:
            _native_rename_at_no_replace(
                reservation.parent_fd,
                owned_name,
                quarantine,
            )
        except FileExistsError:
            continue
        except FileNotFoundError:
            return
        return
    raise OSError("atomic Week 3 report publication failed")


def _remove_known_leaf_or_quarantine(
    reservation: Week3ReportReservation,
    directory_fd: int,
    name: str,
    binding: _FileBinding | None,
) -> None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if binding is not None:
        try:
            _unlink_bound_file(binding)
            return
        except OSError:
            pass
    _quarantine_entry(reservation, directory_fd, name)


def _cleanup_cases_directory(reservation: Week3ReportReservation) -> None:
    if reservation._cases_fd is None or reservation._cases_fd_closed:
        return
    bindings = {
        item.name: item
        for item in reservation._file_bindings
        if item.directory_fd == reservation._cases_fd
    }
    for name in tuple(os.listdir(reservation._cases_fd)):
        _remove_known_leaf_or_quarantine(
            reservation,
            reservation._cases_fd,
            name,
            bindings.get(name),
        )


def _remove_owned(reservation: Week3ReportReservation) -> None:
    name = _find_owned_name(reservation)
    if name is None or reservation._staging_fd_closed:
        return
    if _directory_handle_identity(reservation.staging_fd) != reservation.staging_identity:
        return
    _cleanup_cases_directory(reservation)
    root_bindings = {
        item.name: item
        for item in reservation._file_bindings
        if item.directory_fd == reservation.staging_fd
    }
    for entry in tuple(os.listdir(reservation.staging_fd)):
        metadata = os.stat(entry, dir_fd=reservation.staging_fd, follow_symlinks=False)
        if (
            reservation._cases_identity is not None
            and _identity(metadata) == reservation._cases_identity
            and stat.S_ISDIR(metadata.st_mode)
            and reservation._cases_fd is not None
            and _rmdir_bound_directory(
                reservation.staging_fd,
                entry,
                identity=reservation._cases_identity,
                held_fd=reservation._cases_fd,
            )
        ):
            continue
        else:
            _remove_known_leaf_or_quarantine(
                reservation,
                reservation.staging_fd,
                entry,
                root_bindings.get(entry),
            )
    if not _rmdir_bound_directory(
        reservation.parent_fd,
        name,
        identity=reservation.staging_identity,
        held_fd=reservation.staging_fd,
    ):
        _quarantine_owned_root(reservation)


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
        _is_complete_sql(value) or _URL.search(value) or _CREDENTIAL.search(value)
    ):
        raise ValueError("Week 3 report contains unsafe metadata")


def _is_complete_sql(value: str) -> bool:
    if _SQL_STRUCTURAL_STATEMENT.search(value) is not None:
        return True
    if _SQL_QUERY_CANDIDATE.search(value) is None:
        return False
    try:
        statements = sqlglot.parse(value, read="postgres")
    except sqlglot.errors.SqlglotError:
        return False
    return any(isinstance(statement, exp.Query) for statement in statements)


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


def _close_verification_fd(reservation: Week3ReportReservation, fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        reservation._close_unknown = True


def _validate_file_binding(
    reservation: Week3ReportReservation,
    binding: _FileBinding,
) -> None:
    if binding.closed:
        raise OSError("Week 3 report inventory changed")
    verification_fd = _open_regular_at(binding.directory_fd, binding.name)
    try:
        _validate_opened_binding(binding, verification_fd)
        final_entry = os.stat(
            binding.name,
            dir_fd=binding.directory_fd,
            follow_symlinks=False,
        )
        if _identity(final_entry) != binding.identity or final_entry.st_size != binding.size:
            raise OSError("Week 3 report inventory changed")
    finally:
        _close_verification_fd(reservation, verification_fd)


def _validate_file_bindings(reservation: Week3ReportReservation) -> None:
    try:
        for binding in reservation._file_bindings:
            _validate_file_binding(reservation, binding)
    except OSError:
        reservation._foreign_content = True
        raise


def _validate_inventory_content(
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


def _validate_inventory(
    reservation: Week3ReportReservation,
    cases_fd: int,
    case_names: tuple[str, ...],
    *,
    published: bool = False,
) -> None:
    try:
        _validate_inventory_content(
            reservation,
            cases_fd,
            case_names,
            published=published,
        )
    except OSError:
        reservation._foreign_content = True
        raise


def _quarantine_final(reservation: Week3ReportReservation) -> None:
    _quarantine_owned_root(reservation)


def _raise_publish_error(error_number: int) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError("Week 3 report already exists or is reserved") from None
    raise OSError("atomic Week 3 report publication failed") from None


def _native_rename_between_no_replace(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
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
            source_fd,
            os.fsencode(source),
            destination_fd,
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
            source_fd,
            os.fsencode(source),
            destination_fd,
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


def _native_rename_at_no_replace(parent_fd: int, source: str, destination: str) -> None:
    _native_rename_between_no_replace(parent_fd, source, parent_fd, destination)


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


def write_week3_report(
    reservation: Week3ReportReservation,
    report: Week3RunReport,
    *,
    publication_evidence: Week3PublicationEvidence | None = None,
) -> PublishedWeek3Report:
    try:
        report = Week3RunReport.model_validate(
            report.model_dump(exclude_computed_fields=True), strict=True
        )
        if publication_evidence is None:
            if report.report_scope == "canonical":
                raise ValueError("canonical report requires independent publication evidence")
        else:
            from governed_analytics.evals.week3.runner import (
                _rebuild_report_from_publication_evidence,
            )

            rebuilt = _rebuild_report_from_publication_evidence(publication_evidence)
            if report.model_dump(exclude_computed_fields=True) != rebuilt.model_dump(
                exclude_computed_fields=True
            ):
                raise ValueError("report does not match independent publication evidence")
            report = rebuilt
        report_value = _dump(report)
        case_values = tuple(_dump(case) for case in report.cases)
    except Exception:
        cancel_week3_report_reservation(reservation)
        raise ValueError("Week 3 report contains unsafe metadata") from None
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
        _validate_published(reservation)
    except BaseException as error:
        primary = error
        raise
    finally:
        if primary is not None:
            with suppress(OSError):
                _remove_owned(reservation)
            reservation._active = False
            with suppress(OSError):
                reservation.close()
    if not published:
        raise OSError("atomic Week 3 report publication failed")
    with suppress(OSError):
        reservation.close()
    reservation._active = False
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
