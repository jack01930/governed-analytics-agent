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
from collections.abc import Sequence
from contextlib import suppress
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
_POINTER_THREAD_LOCKS: dict[str, threading.Lock] = {}


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


def _open_pointer_parent(path: Path) -> tuple[int, str]:
    absolute = path.absolute()
    name = absolute.name
    if not name or name in {".", ".."}:
        raise OSError("atomic evaluation pointer unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parent.parts[1:]:
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                child_fd = os.open(part, flags, dir_fd=current_fd)
            old_fd, current_fd = current_fd, child_fd
            os.close(old_fd)
        return current_fd, name
    except BaseException:
        with suppress(OSError):
            os.close(current_fd)
        raise


def _file_identity_at(directory_fd: int, name: str) -> tuple[int, int, int] | None:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise OSError("atomic evaluation pointer unavailable")
    return status.st_dev, status.st_ino, status.st_size


def _file_bytes_at(directory_fd: int, name: str) -> bytes:
    file_fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=directory_fd,
    )
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise OSError("atomic evaluation pointer unavailable")
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 8192):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _atomic_write_text_locked(path: str | Path, contents: str) -> None:
    """Atomically replace one local pointer without following path symlinks.

    Once rename has exposed and revalidated the exact staged inode, a parent
    fsync error is treated as published-but-durability-ambiguous. Re-running a
    paid evaluation merely to repair that ambiguity would be less safe than
    accepting the already visible, byte-complete pointer.
    """
    directory_fd = -1
    lock_fd = -1
    temporary_name: str | None = None
    temporary_identity: tuple[int, int, int] | None = None
    expected_bytes = contents.encode("utf-8")
    try:
        directory_fd, target_name = _open_pointer_parent(Path(path))
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
        status = os.fstat(temporary_fd)
        temporary_identity = (status.st_dev, status.st_ino, status.st_size)
        with os.fdopen(temporary_fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            status = os.fstat(stream.fileno())
            temporary_identity = (status.st_dev, status.st_ino, status.st_size)
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
            if (
                _file_identity_at(directory_fd, target_name) != temporary_identity
                or _file_bytes_at(directory_fd, target_name) != expected_bytes
            ):
                raise
        temporary_name = None
        if (
            _file_identity_at(directory_fd, target_name) != temporary_identity
            or _file_bytes_at(directory_fd, target_name) != expected_bytes
        ):
            raise OSError("atomic evaluation pointer unavailable")
        try:
            os.fsync(directory_fd)
        except OSError:
            if (
                _file_identity_at(directory_fd, target_name) != temporary_identity
                or _file_bytes_at(directory_fd, target_name) != expected_bytes
            ):
                raise OSError("atomic evaluation pointer unavailable") from None
        if (
            _file_identity_at(directory_fd, target_name) != temporary_identity
            or _file_bytes_at(directory_fd, target_name) != expected_bytes
        ):
            raise OSError("atomic evaluation pointer unavailable")
    except OSError:
        raise OSError("atomic evaluation pointer unavailable") from None
    finally:
        if temporary_name is not None and directory_fd >= 0:
            with suppress(OSError):
                if _file_identity_at(directory_fd, temporary_name) == temporary_identity:
                    os.unlink(temporary_name, dir_fd=directory_fd)
        if lock_fd >= 0:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(lock_fd)
        if directory_fd >= 0:
            with suppress(OSError):
                os.close(directory_fd)


def _atomic_write_text(path: str | Path, contents: str) -> None:
    key = str(Path(path).absolute())
    with _POINTER_THREAD_LOCKS_GUARD:
        thread_lock = _POINTER_THREAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        _atomic_write_text_locked(path, contents)


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
