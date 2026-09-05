"""One-shot local tiny-dataset freezer for the 19 independent Week 3 Oracles."""

from __future__ import annotations

import argparse
import asyncio
import errno
import math
import os
import platform
import shutil
import stat
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import text

from governed_analytics.config import DatabaseSettings
from governed_analytics.evals.models import QueryResult as EvalQueryResult
from governed_analytics.evals.week3.models import FrozenExpectedResult
from governed_analytics.evals.week3.suites import (
    WEEK3_EXPECTED_ROOT,
    WEEK3_ORACLE_ROOT,
)
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.safety.sql_policy import validate_sql

_ORACLE_STEMS = tuple(
    [f"W3K{number:03d}" for number in range(11, 26)]
    + [
        "W3K026-confirm_decline",
        "W3K026-region_contribution",
        "W3K026-sku_contribution",
        "W3K026-segment_contribution",
    ]
)


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    mode: int


@dataclass(slots=True)
class _OwnedStaging:
    path: Path
    parent: Path
    identity: _DirectoryIdentity
    parent_identity: _DirectoryIdentity
    directory_fd: int
    parent_fd: int
    directory_fd_closed: bool = False
    parent_fd_closed: bool = False

    @property
    def closed(self) -> bool:
        return self.directory_fd_closed and self.parent_fd_closed

    def close(self) -> None:
        if self.closed:
            return
        first_error: OSError | None = None
        if not self.directory_fd_closed:
            try:
                os.close(self.directory_fd)
            except OSError as error:
                first_error = error
            else:
                self.directory_fd_closed = True
        if not self.parent_fd_closed:
            try:
                os.close(self.parent_fd)
            except OSError as error:
                if first_error is None:
                    first_error = error
            else:
                self.parent_fd_closed = True
        if first_error is not None:
            raise first_error


def _require_real_directory(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Week 3 output paths cannot contain symlinks")
    if not path.is_dir():
        raise ValueError("Week 3 expected output parent must be a real directory")


def _directory_identity(path: Path) -> _DirectoryIdentity:
    try:
        metadata = path.lstat()
    except OSError:
        raise RuntimeError("Week 3 staging directory identity changed") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Week 3 staging directory identity changed")
    return _identity_from_stat(metadata)


def _identity_from_stat(metadata: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
    )


def _handle_identity(file_descriptor: int) -> _DirectoryIdentity:
    try:
        metadata = os.fstat(file_descriptor)
    except OSError:
        raise RuntimeError("Week 3 staging directory identity changed") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Week 3 staging directory identity changed")
    return _identity_from_stat(metadata)


def _open_directory_no_follow(path: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("secure Week 3 staging handles are unavailable")
    flags = os.O_RDONLY | no_follow
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except OSError:
        raise RuntimeError("Week 3 staging directory identity changed") from None


def _validate_owned_staging(owned: _OwnedStaging, *, require_path: bool = True) -> None:
    if owned.closed:
        raise RuntimeError("Week 3 staging directory identity changed")
    try:
        _require_real_directory(owned.parent)
    except ValueError:
        raise RuntimeError("Week 3 staging directory identity changed") from None
    if (
        _handle_identity(owned.parent_fd) != owned.parent_identity
        or _directory_identity(owned.parent) != owned.parent_identity
        or _handle_identity(owned.directory_fd) != owned.identity
        or (require_path and _directory_identity(owned.path) != owned.identity)
    ):
        raise RuntimeError("Week 3 staging directory identity changed")


def _create_owned_staging(parent: Path) -> _OwnedStaging:
    _require_real_directory(parent)
    parent_path_identity = _directory_identity(parent)
    parent_fd = _open_directory_no_follow(parent)
    staging: Path | None = None
    created_identity: _DirectoryIdentity | None = None
    directory_fd: int | None = None
    try:
        parent_identity = _handle_identity(parent_fd)
        if parent_identity != parent_path_identity:
            raise RuntimeError("Week 3 staging directory identity changed")
        staging = Path(tempfile.mkdtemp(prefix=".week3-expected-", dir=parent))
        created_identity = _directory_identity(staging)
        directory_fd = _open_directory_no_follow(staging)
        identity = _handle_identity(directory_fd)
        if identity != created_identity or stat.S_IMODE(identity.mode) != 0o700:
            raise RuntimeError("Week 3 staging directory identity changed")
        owned = _OwnedStaging(
            path=staging,
            parent=parent,
            identity=identity,
            parent_identity=parent_identity,
            directory_fd=directory_fd,
            parent_fd=parent_fd,
        )
        _validate_owned_staging(owned)
        return owned
    except BaseException:
        try:
            if directory_fd is not None:
                with suppress(OSError):
                    os.close(directory_fd)
        finally:
            try:
                with suppress(OSError):
                    os.close(parent_fd)
            finally:
                if staging is not None and created_identity is not None:
                    try:
                        if _directory_identity(staging) == created_identity:
                            shutil.rmtree(staging)
                    except (OSError, RuntimeError):
                        pass
        raise


def _remove_owned_staging(owned: _OwnedStaging) -> None:
    _validate_owned_staging(owned)
    shutil.rmtree(owned.path)


def _clean_owned_staging(owned: _OwnedStaging) -> None:
    try:
        _validate_owned_staging(owned)
        _remove_owned_staging(owned)
    except (OSError, RuntimeError):
        return


def _oracle_inventory() -> tuple[Path, ...]:
    wanted = tuple(WEEK3_ORACLE_ROOT / f"{stem}.sql" for stem in _ORACLE_STEMS)
    try:
        current = (
            Path(WEEK3_ORACLE_ROOT.anchor) if WEEK3_ORACLE_ROOT.is_absolute() else Path()
        )
        for part in (
            WEEK3_ORACLE_ROOT.parts[1:]
            if WEEK3_ORACLE_ROOT.is_absolute()
            else WEEK3_ORACLE_ROOT.parts
        ):
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError
        if not stat.S_ISDIR(WEEK3_ORACLE_ROOT.stat(follow_symlinks=False).st_mode):
            raise ValueError
        actual = set(WEEK3_ORACLE_ROOT.rglob("*"))
        if actual != set(wanted):
            raise ValueError
        for path in wanted:
            current = Path(path.anchor) if path.is_absolute() else Path()
            for part in path.parts[1:] if path.is_absolute() else path.parts:
                current /= part
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ValueError
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise ValueError
    except (OSError, ValueError):
        raise ValueError("Week 3 Oracle inventory is unavailable") from None
    return wanted


def _collision_error() -> FileExistsError:
    return FileExistsError("Week 3 expected output already exists; refusing overwrite")


def _raise_publish_error(error_number: int) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise _collision_error() from None
    raise OSError("atomic Week 3 expected publication failed") from None


def _publish_darwin_no_replace(staging: Path, destination: Path) -> None:
    import ctypes

    try:
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
    except AttributeError:
        raise OSError("atomic Week 3 expected publication unavailable") from None
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex_np.restype = ctypes.c_int
    ctypes.set_errno(0)
    if renamex_np(os.fsencode(staging), os.fsencode(destination), 0x00000004) != 0:
        _raise_publish_error(ctypes.get_errno())


def _publish_linux_no_replace(staging: Path, destination: Path) -> None:
    import ctypes

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError("atomic Week 3 expected publication unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    if renameat2(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
        _raise_publish_error(ctypes.get_errno())


def _publish_windows_no_replace(staging: Path, destination: Path) -> None:
    import ctypes

    win_dll: Any = getattr(ctypes, "WinDLL", None)
    set_last_error: Any = getattr(ctypes, "set_last_error", None)
    get_last_error: Any = getattr(ctypes, "get_last_error", None)
    if win_dll is None or set_last_error is None or get_last_error is None:
        raise OSError("atomic Week 3 expected publication unavailable")
    move_file_ex = win_dll("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    move_file_ex.restype = ctypes.c_int
    set_last_error(0)
    if not move_file_ex(str(staging), str(destination), 0x00000008):
        if get_last_error() in {80, 183}:
            raise _collision_error() from None
        raise OSError("atomic Week 3 expected publication failed") from None


def _rename_path_no_replace(source: Path, destination: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        _publish_darwin_no_replace(source, destination)
    elif system == "Linux":
        _publish_linux_no_replace(source, destination)
    elif system == "Windows":
        _publish_windows_no_replace(source, destination)
    else:
        raise OSError("atomic Week 3 expected publication unavailable")


def _native_publish_no_replace(owned: _OwnedStaging, destination: Path) -> None:
    _validate_owned_staging(owned)
    _rename_path_no_replace(owned.path, destination)


def _quarantine_mismatched_destination(destination: Path) -> Path:
    for _attempt in range(16):
        quarantine = destination.parent / f".week3-quarantine-{uuid4().hex}"
        try:
            _rename_path_no_replace(destination, quarantine)
        except FileExistsError:
            continue
        return quarantine
    raise RuntimeError("Week 3 staging directory identity changed")


def _publish_staged_directory(owned: _OwnedStaging, destination: Path) -> None:
    _native_publish_no_replace(owned, destination)
    try:
        _validate_owned_staging(owned, require_path=False)
        if _directory_identity(destination) != owned.identity:
            raise RuntimeError("Week 3 staging directory identity changed")
    except RuntimeError:
        _quarantine_mismatched_destination(destination)
        raise RuntimeError("Week 3 staging directory identity changed") from None


def _json_value(value: object) -> object:
    if value is None or type(value) in {str, int, bool}:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Oracle decimals must be finite")
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Oracle floats must be finite")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Oracle datetimes must be timezone-aware")
        rendered = value.astimezone(UTC).isoformat()
        return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    raise ValueError(f"unsupported Oracle result value: {type(value).__name__}")


async def _freeze_to_staging(staging: Path) -> tuple[str, ...]:
    oracle_paths = _oracle_inventory()
    settings = DatabaseSettings()  # type: ignore[call-arg]
    engine = create_async_database_engine(settings)
    written: list[str] = []
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("set transaction isolation level repeatable read, read only")
            )
            await connection.execute(text("set local statement_timeout = '10s'"))
            await connection.execute(text("set local search_path = public, pg_catalog"))
            await connection.execute(text("set local time zone 'UTC'"))
            identity = await connection.execute(
                text(
                    "select current_user, current_setting('transaction_read_only'), "
                    "current_setting('search_path'), current_setting('TimeZone')"
                )
            )
            if tuple(identity.one()) != ("analytics_readonly", "on", "public, pg_catalog", "UTC"):
                raise RuntimeError("Week 3 freezer requires the hardened analytics_readonly role")

            for stem, oracle_path in zip(_ORACLE_STEMS, oracle_paths, strict=True):
                validated = validate_sql(oracle_path.read_text(encoding="utf-8"))
                if validated.parameter_names:
                    raise ValueError("Week 3 Oracles must contain fixed timestamptz literals")
                execution = await connection.exec_driver_sql(
                    validated.driver_sql or validated.sql,
                    (),
                )
                columns = tuple(map(str, execution.keys()))
                rows = tuple(
                    tuple(_json_value(value) for value in row) for row in execution.fetchall()
                )
                frozen = FrozenExpectedResult(
                    oracle_query_id=validated.query_id,
                    result=EvalQueryResult(columns=columns, rows=rows),
                )
                destination = staging / f"{stem}.json"
                destination.write_text(
                    frozen.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
                reloaded = FrozenExpectedResult.model_validate_json(
                    destination.read_text(encoding="utf-8"), strict=True
                )
                if reloaded != frozen:
                    raise RuntimeError("frozen Week 3 result failed strict round-trip")
                written.append(destination.name)
    finally:
        await engine.dispose()
    if tuple(written) != tuple(f"{stem}.json" for stem in _ORACLE_STEMS):
        raise RuntimeError("Week 3 freezer did not produce the fixed 19-result inventory")
    return tuple(written)


def freeze_week3_expected(
    *,
    dataset: Literal["tiny"],
    output_root: Path = WEEK3_EXPECTED_ROOT,
) -> tuple[Path, ...]:
    """Freeze all 19 Oracle results once, then atomically publish the whole directory."""
    if dataset != "tiny":
        raise ValueError("Week 3 expected results may be frozen only from dataset tiny")
    output_root = Path(output_root)
    if output_root.exists() or output_root.is_symlink():
        raise FileExistsError("Week 3 expected output already exists; refusing overwrite")
    parent = output_root.parent
    _require_real_directory(parent)
    _oracle_inventory()
    owned = _create_owned_staging(parent)
    published = False
    try:
        names = asyncio.run(_freeze_to_staging(owned.path))
        _publish_staged_directory(owned, output_root)
        published = True
        return tuple(output_root / name for name in names)
    except BaseException:
        if not published:
            _clean_owned_staging(owned)
        raise
    finally:
        owned.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze Week 3 tiny Oracle results once")
    parser.add_argument("--dataset", required=True, choices=("tiny",))
    args = parser.parse_args(argv)
    paths = freeze_week3_expected(dataset=cast(Literal["tiny"], args.dataset))
    if len(paths) != 19:
        raise RuntimeError("unexpected Week 3 expected result count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["freeze_week3_expected", "main"]
