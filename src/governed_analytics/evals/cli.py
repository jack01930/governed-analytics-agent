"""Explicitly authorized command-line entry point for baseline evaluation."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import stat
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Literal, Never, cast
from uuid import uuid4

from openai import AsyncOpenAI

from governed_analytics.config import ModelSettings
from governed_analytics.evals.pricing import ModelPricing, load_model_pricing
from governed_analytics.evals.runner import BaselineRunError, run_baseline
from governed_analytics.evals.week2_models import Week2RunReport
from governed_analytics.evals.week2_runner import Week2RunError, run_week2_evaluation
from governed_analytics.models.openai_compatible import OpenAICompatibleSqlGenerator
from governed_analytics.models.protocols import EvaluationSqlGenerator, SqlGenerator

_PRICING_PATH = "data/pricing/deepseek-v4-flash-2026-09-01.yaml"
_POINTER_THREAD_LOCKS_GUARD = threading.Lock()
_POINTER_THREAD_LOCKS: dict[tuple[int, int, str], threading.Lock] = {}


@dataclass(frozen=True)
class _PointerParent:
    directory_fd: int
    target_name: str
    requested_parent: Path
    identity: tuple[int, int]


def _lexical_absolute(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


class _CliArgumentError(ValueError):
    """Internal marker for a public, input-free argparse failure."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise _CliArgumentError


def _parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(prog="governed-eval")
    command = parser.add_subparsers(dest="command", required=True)
    baseline = command.add_parser("baseline")
    baseline.add_argument("--dataset", required=True, choices=("tiny", "full"))
    baseline.add_argument("--mode", required=True, choices=("fixture", "live"))
    baseline.add_argument("--live", action="store_true")
    week2 = command.add_parser("week2")
    week2.add_argument("--dataset", required=True, choices=("tiny", "full"))
    week2.add_argument("--mode", required=True, choices=("fixture", "live"))
    week2.add_argument("--live", action="store_true")
    week2.add_argument("--summary-file", type=Path)
    week3 = command.add_parser("week3")
    week3.add_argument("--dataset", required=True, choices=("tiny", "full"))
    week3.add_argument("--mode", required=True, choices=("fixture", "live"))
    week3.add_argument("--live", action="store_true")
    week3.add_argument("--report-path-file", type=Path)
    return parser


def _failure(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _open_directory_nofollow(path: Path, *, create_missing: bool) -> int:
    absolute = _lexical_absolute(path)
    if not absolute.is_absolute():
        raise OSError("atomic evaluation pointer unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child_fd = -1
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create_missing:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                child_fd = os.open(part, flags, dir_fd=current_fd)
            old_fd = current_fd
            current_fd = child_fd
            os.close(old_fd)
        return current_fd
    except BaseException:
        with suppress(OSError):
            os.close(current_fd)
        raise


def _open_pointer_parent(path: Path) -> _PointerParent:
    absolute = _lexical_absolute(path)
    name = absolute.name
    if not name or name in {".", ".."}:
        raise OSError("atomic evaluation pointer unavailable")
    directory_fd = _open_directory_nofollow(absolute.parent, create_missing=True)
    owns_directory_fd = True
    try:
        status = os.fstat(directory_fd)
        parent = _PointerParent(
            directory_fd=directory_fd,
            target_name=name,
            requested_parent=absolute.parent,
            identity=(status.st_dev, status.st_ino),
        )
        owns_directory_fd = False
        return parent
    finally:
        if owns_directory_fd:
            with suppress(OSError):
                os.close(directory_fd)


def _verify_pointer_parent(parent: _PointerParent) -> None:
    try:
        held_status = os.fstat(parent.directory_fd)
        if (held_status.st_dev, held_status.st_ino) != parent.identity:
            raise OSError
        check_fd = _open_directory_nofollow(parent.requested_parent, create_missing=False)
        try:
            check_status = os.fstat(check_fd)
            if (check_status.st_dev, check_status.st_ino) != parent.identity:
                raise OSError
        finally:
            os.close(check_fd)
    except OSError:
        raise OSError("atomic evaluation pointer unavailable") from None


def _close_pointer_parent(parent: _PointerParent) -> None:
    with suppress(OSError):
        os.close(parent.directory_fd)


def _unlink_owned_pointer(
    parent: _PointerParent,
    identity: tuple[int, int, int] | None,
    expected_bytes: bytes,
) -> None:
    if identity is None:
        return
    try:
        _verify_bound_regular_file(
            parent.directory_fd,
            parent.target_name,
            expected_identity=identity,
            expected_bytes=expected_bytes,
            unlink_verified=True,
        )
        with suppress(OSError):
            os.fsync(parent.directory_fd)
    except OSError:
        pass


def _file_identity_at(directory_fd: int, name: str) -> tuple[int, int, int] | None:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise OSError("atomic evaluation pointer unavailable")
    return status.st_dev, status.st_ino, status.st_size


def _verify_bound_regular_file(
    directory_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | tuple[int, int, int] | None = None,
    expected_bytes: bytes | None = None,
    held_fd: int | None = None,
    unlink_verified: bool = False,
) -> tuple[tuple[int, int, int], bytes]:
    file_fd = held_fd
    owns_fd = file_fd is None
    if file_fd is None:
        file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
    try:
        before = os.fstat(file_fd)
        identity = (before.st_dev, before.st_ino, before.st_size)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("atomic evaluation pointer unavailable")
        os.lseek(file_fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 8192):
            chunks.append(chunk)
        contents = b"".join(chunks)
        after = os.fstat(file_fd)
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        after_identity = (after.st_dev, after.st_ino, after.st_size)
        entry_identity = (entry.st_dev, entry.st_ino, entry.st_size)
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(entry.st_mode)
            or after_identity != identity
            or entry_identity != identity
            or (
                expected_identity is not None
                and identity[: len(expected_identity)] != expected_identity
            )
            or (expected_bytes is not None and contents != expected_bytes)
        ):
            raise OSError("atomic evaluation pointer unavailable")
        final_held = os.fstat(file_fd)
        final_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(final_held.st_mode)
            or not stat.S_ISREG(final_entry.st_mode)
            or (final_held.st_dev, final_held.st_ino, final_held.st_size) != identity
            or (final_entry.st_dev, final_entry.st_ino, final_entry.st_size) != identity
        ):
            raise OSError("atomic evaluation pointer unavailable")
        if unlink_verified:
            adjacent = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(adjacent.st_mode)
                or (adjacent.st_dev, adjacent.st_ino, adjacent.st_size) != identity
            ):
                raise OSError("atomic evaluation pointer unavailable")
            os.unlink(name, dir_fd=directory_fd)
        return identity, contents
    finally:
        if owns_fd:
            os.close(file_fd)


def _verify_bound_identity(
    directory_fd: int,
    name: str,
    *,
    held_fd: int,
    expected_identity: tuple[int, int, int],
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    if expected_parent_identity is not None:
        parent = os.fstat(directory_fd)
        if (parent.st_dev, parent.st_ino) != expected_parent_identity:
            raise OSError("atomic evaluation pointer unavailable")
    held = os.fstat(held_fd)
    entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(entry.st_mode)
        or (held.st_dev, held.st_ino, held.st_size) != expected_identity
        or (entry.st_dev, entry.st_ino, entry.st_size) != expected_identity
    ):
        raise OSError("atomic evaluation pointer unavailable")


def _unlink_owned_open_temporary(
    directory_fd: int,
    name: str,
    *,
    held_fd: int,
    expected_identity: tuple[int, int] | None,
) -> bool:
    """Best-effort removal while the newly created inode is still held open."""
    try:
        held = os.fstat(held_fd)
        held_identity = (held.st_dev, held.st_ino)
        if (
            not stat.S_ISREG(held.st_mode)
            or (expected_identity is not None and held_identity != expected_identity)
        ):
            return False
        adjacent = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(adjacent.st_mode) or (
            adjacent.st_dev,
            adjacent.st_ino,
        ) != held_identity:
            return False
        os.unlink(name, dir_fd=directory_fd)
        return True
    except OSError:
        return False


def _pointer_matches(
    parent: _PointerParent,
    identity: tuple[int, int, int] | None,
    expected_bytes: bytes,
) -> bool:
    if identity is None:
        return False
    try:
        _verify_bound_regular_file(
            parent.directory_fd,
            parent.target_name,
            expected_identity=identity,
            expected_bytes=expected_bytes,
        )
    except OSError:
        return False
    return True


def _atomic_write_text_locked(
    parent: _PointerParent,
    contents: str,
    *,
    publication_guard: Callable[[], None] | None = None,
    final_publication_guard: Callable[[], None] | None = None,
) -> None:
    """Atomically replace one local pointer without following path symlinks.

    Once rename has exposed and revalidated the exact staged inode, a parent
    fsync error is treated as published-but-durability-ambiguous. Re-running a
    paid evaluation merely to repair that ambiguity would be less safe than
    accepting the already visible, byte-complete pointer.
    """
    directory_fd = parent.directory_fd
    lock_fd = -1
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    staged_identity: tuple[int, int, int] | None = None
    expected_bytes = contents.encode("utf-8")
    published_identity: tuple[int, int, int] | None = None
    try:
        _verify_pointer_parent(parent)
        target_name = parent.target_name
        lock_digest = sha256(target_name.encode("utf-8")).hexdigest()[:24]
        lock_name = f".eval-pointer-{lock_digest}.lock"
        lock_fd = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        lock_status = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_status.st_mode):
            raise OSError("atomic evaluation pointer unavailable")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        original_identity = _file_identity_at(directory_fd, target_name)
        temporary_name = f".eval-pointer-{uuid4().hex}.tmp"
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        owns_temporary_fd = True
        try:
            try:
                status = os.fstat(temporary_fd)
                temporary_identity = (status.st_dev, status.st_ino)
                stream = os.fdopen(temporary_fd, "w", encoding="utf-8", newline="")
            except BaseException:
                if _unlink_owned_open_temporary(
                    directory_fd,
                    temporary_name,
                    held_fd=temporary_fd,
                    expected_identity=temporary_identity,
                ):
                    temporary_name = None
                raise
            owns_temporary_fd = False
            with stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
                status = os.fstat(stream.fileno())
                staged_identity = (status.st_dev, status.st_ino, status.st_size)
        finally:
            if owns_temporary_fd:
                with suppress(OSError):
                    os.close(temporary_fd)
        _verify_pointer_parent(parent)
        if publication_guard is not None:
            publication_guard()
        if _file_identity_at(directory_fd, target_name) != original_identity:
            raise OSError("atomic evaluation pointer unavailable")
        try:
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError:
            if not _pointer_matches(parent, staged_identity, expected_bytes):
                raise
        temporary_name = None
        published_identity = staged_identity
        if not _pointer_matches(parent, staged_identity, expected_bytes):
            raise OSError("atomic evaluation pointer unavailable")
        _verify_pointer_parent(parent)
        if publication_guard is not None:
            publication_guard()
        try:
            os.fsync(directory_fd)
        except OSError:
            if not _pointer_matches(parent, staged_identity, expected_bytes):
                raise OSError("atomic evaluation pointer unavailable") from None
        _verify_pointer_parent(parent)
        if staged_identity is None:
            raise OSError("atomic evaluation pointer unavailable")
        final_fd = os.open(
            target_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        try:
            _verify_bound_regular_file(
                directory_fd,
                target_name,
                expected_identity=staged_identity,
                expected_bytes=expected_bytes,
                held_fd=final_fd,
            )
            if final_publication_guard is not None:
                final_publication_guard()
            _verify_bound_identity(
                directory_fd,
                target_name,
                held_fd=final_fd,
                expected_identity=staged_identity,
                expected_parent_identity=parent.identity,
            )
        finally:
            os.close(final_fd)
    except OSError:
        _unlink_owned_pointer(parent, published_identity, expected_bytes)
        raise OSError("atomic evaluation pointer unavailable") from None
    finally:
        if temporary_name is not None and directory_fd >= 0:
            with suppress(OSError):
                if temporary_identity is not None:
                    _verify_bound_regular_file(
                        directory_fd,
                        temporary_name,
                        expected_identity=temporary_identity,
                        unlink_verified=True,
                    )
        if lock_fd >= 0:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(lock_fd)


def _atomic_write_text(
    path: str | Path,
    contents: str,
    *,
    publication_guard: Callable[[], None] | None = None,
    final_publication_guard: Callable[[], None] | None = None,
) -> None:
    parent: _PointerParent | None = None
    try:
        parent = _open_pointer_parent(Path(path))
        key = (*parent.identity, parent.target_name)
        with _POINTER_THREAD_LOCKS_GUARD:
            thread_lock = _POINTER_THREAD_LOCKS.setdefault(key, threading.Lock())
        with thread_lock:
            _verify_pointer_parent(parent)
            _atomic_write_text_locked(
                parent,
                contents,
                publication_guard=publication_guard,
                final_publication_guard=final_publication_guard,
            )
    except OSError:
        raise OSError("atomic evaluation pointer unavailable") from None
    finally:
        if parent is not None:
            _close_pointer_parent(parent)


def _week2_summary(report: Week2RunReport) -> str:
    run_id = report.run_id
    suite_hash = report.suite_manifest_sha256
    try:
        safety_rate = Decimal(report.safety_rejection_rate)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Week 2 summary is unavailable") from None
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None
        or not isinstance(suite_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", suite_hash) is None
        or not Decimal("0") <= safety_rate <= Decimal("1")
    ):
        raise ValueError("Week 2 summary is unavailable")
    return (
        json.dumps(
            {
                "run_id": run_id,
                "safety_rejection_rate": str(safety_rate),
                "suite_manifest_sha256": suite_hash,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _run(
    *,
    mode: Literal["fixture", "live"],
    generator: SqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    client: AsyncOpenAI | None = None,
) -> None:
    async def execute() -> None:
        try:
            await run_baseline(mode=mode, generator=generator, pricing=pricing)
        finally:
            if client is not None:
                await client.close()

    try:
        asyncio.run(execute())
    except BaselineRunError:
        raise
    except Exception:
        raise BaselineRunError("baseline client cleanup failed") from None


def _run_week2(
    *,
    mode: Literal["fixture", "live"],
    generator: EvaluationSqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    client: AsyncOpenAI | None = None,
) -> Week2RunReport:
    async def execute() -> Week2RunReport:
        try:
            return await run_week2_evaluation(mode=mode, generator=generator, pricing=pricing)
        finally:
            if client is not None:
                # The report may already contain 50 paid, immutable outcomes.
                # A best-effort transport cleanup must never relabel that run as
                # failed and encourage an accidental paid rerun.
                with suppress(Exception):
                    await client.close()

    try:
        return asyncio.run(execute())
    except Week2RunError:
        raise
    except Exception:
        raise Week2RunError("Week 2 client cleanup failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _CliArgumentError:
        return _failure("Invalid governed-eval arguments")
    if args.command == "week3":
        from governed_analytics.evals.week3 import cli as week3_cli

        try:
            week3_cli.run_week3_command(
                dataset=cast(Literal["tiny", "full"], args.dataset),
                mode=cast(Literal["fixture", "live"], args.mode),
                live=cast(bool, args.live),
                report_path_file=cast(Path | None, args.report_path_file),
            )
        except week3_cli.Week3CliError as error:
            return _failure(str(error))
        return 0
    is_week2 = args.command == "week2"
    if args.dataset != "tiny":
        return _failure(
            "Week 2 evaluation supports only dataset tiny"
            if is_week2
            else "Week 1 baseline supports only dataset tiny"
        )
    if args.mode == "fixture":
        try:
            if is_week2:
                report = _run_week2(mode="fixture")
                if args.summary_file is not None:
                    _atomic_write_text(args.summary_file, _week2_summary(report))
            else:
                _run(mode="fixture")
        except OSError:
            return _failure("Week 2 summary publication failed")
        except (BaselineRunError, Week2RunError, ValueError):
            return _failure(
                "Fixture Week 2 evaluation failed" if is_week2 else "Fixture baseline failed"
            )
        return 0
    if not args.live:
        return _failure("Live model calls require --live")
    try:
        settings = ModelSettings()
    except Exception:
        return _failure("MODEL settings are invalid")
    secret = settings.model_api_key
    if secret is None:
        return _failure("MODEL_API_KEY is not configured")
    try:
        pricing = load_model_pricing(_PRICING_PATH)
    except Exception:
        return _failure("MODEL pricing is unavailable")
    if (
        pricing.requested_model != settings.model_name
        or pricing.resolved_model != settings.eval_model_name
    ):
        return _failure("MODEL pricing is unavailable")
    client: AsyncOpenAI | None = None
    try:
        client = AsyncOpenAI(
            api_key=secret.get_secret_value(),
            base_url=settings.model_base_url,
            max_retries=0,
        )
        generator = OpenAICompatibleSqlGenerator(client, settings.model_name)
    except Exception:
        if client is not None:
            with suppress(Exception):
                asyncio.run(client.close())
        return _failure("MODEL client is unavailable")
    try:
        if is_week2:
            report = _run_week2(mode="live", generator=generator, pricing=pricing, client=client)
            if args.summary_file is not None:
                _atomic_write_text(args.summary_file, _week2_summary(report))
        else:
            _run(mode="live", generator=generator, pricing=pricing, client=client)
    except OSError:
        return _failure("Week 2 summary publication failed")
    except (BaselineRunError, Week2RunError):
        return _failure("Live Week 2 evaluation failed" if is_week2 else "Live baseline failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
