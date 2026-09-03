"""Immutable JSON and Markdown baseline-report publication."""

from __future__ import annotations

import errno
import json
import os
import platform
import shutil
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from governed_analytics.evals.models import (
    _EXPECTED_EVALUATION_CASE_IDS,
    BaselineCaseResult,
    BaselineRunReport,
)

_RATE_QUANTUM = Decimal("0.0001")
_COST_QUANTUM = Decimal("0.000001")
_STATUSES = ("passed", "wrong_answer", "invalid_sql", "execution_error")


def build_baseline_run_report(
    cases: Sequence[BaselineCaseResult],
    *,
    run_id: str,
    mode: Literal["fixture", "live"],
    dataset_id: str,
    model: str,
    prompt_version: str,
    requested_model: str,
    resolved_models: tuple[str, ...] = (),
    pricing_effective_date: date | None = None,
    pricing_basis: str | None = None,
) -> BaselineRunReport:
    """Build exact aggregate metrics from one complete set of case outcomes."""
    case_results = tuple(cases)
    case_ids = tuple(case.case_id for case in case_results)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("baseline reports cannot contain duplicate case IDs")
    if case_ids != _EXPECTED_EVALUATION_CASE_IDS:
        raise ValueError("baseline reports require exactly 20 ordered G001 through G020 cases")
    total_cases = Decimal(len(case_results))
    return BaselineRunReport(
        run_id=run_id,
        mode=mode,
        dataset_id=dataset_id,
        model=model,
        prompt_version=prompt_version,
        requested_model=requested_model,
        resolved_models=resolved_models,
        pricing_effective_date=pricing_effective_date,
        pricing_basis=pricing_basis,
        result_accuracy=sum((case.score for case in case_results), Decimal("0")) / total_cases,
        valid_sql_rate=(
            Decimal(sum(case.status != "invalid_sql" for case in case_results)) / total_cases
        ),
        execution_success_rate=(
            Decimal(sum(case.status in {"passed", "wrong_answer"} for case in case_results))
            / total_cases
        ),
        total_cost_cny=sum((case.estimated_cost_cny for case in case_results), Decimal("0")),
        cases=case_results,
    )


def _serialise(model: BaselineRunReport | BaselineCaseResult) -> str:
    return (
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )


def _write_text(path: Path, contents: str) -> None:
    """Write deterministic UTF-8 text; separately patchable for failure-boundary tests."""
    path.write_text(contents, encoding="utf-8", newline="\n")


def _format_decimal(value: Decimal, quantum: Decimal) -> str:
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _escape_markdown(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _nearest_rank(latencies: Sequence[int], percentile: int) -> int:
    if not latencies:
        return 0
    ordered = sorted(latencies)
    rank = (len(ordered) * percentile + 99) // 100
    return ordered[rank - 1]


def _render_markdown(report: BaselineRunReport) -> str:
    status_counts: Counter[str] = Counter(case.status for case in report.cases)
    average_cost = report.total_cost_cny / Decimal(len(report.cases))
    lines = ["# Baseline evaluation report", ""]
    if report.mode == "fixture":
        lines.extend(["**Harness validation, not model quality.**", ""])
    else:
        lines.extend(
            [
                f"- Pricing effective date: {report.pricing_effective_date}",
                f"- Pricing basis: {_escape_markdown(report.pricing_basis)}",
            ]
        )
    lines.extend(
        [
            f"- Run ID: {_escape_markdown(report.run_id)}",
            f"- Mode: {_escape_markdown(report.mode)}",
            f"- Dataset ID: {_escape_markdown(report.dataset_id)}",
            f"- Prompt version: {_escape_markdown(report.prompt_version)}",
            f"- Requested model: {_escape_markdown(report.requested_model)}",
            "- Resolved models: " + _escape_markdown(", ".join(report.resolved_models) or "(none)"),
            f"- Result accuracy: {_format_decimal(report.result_accuracy, _RATE_QUANTUM)}",
            f"- Valid SQL rate: {_format_decimal(report.valid_sql_rate, _RATE_QUANTUM)}",
            "- Execution success rate: "
            f"{_format_decimal(report.execution_success_rate, _RATE_QUANTUM)}",
            f"- Total cost (CNY): {_format_decimal(report.total_cost_cny, _COST_QUANTUM)}",
            f"- Average cost (CNY): {_format_decimal(average_cost, _COST_QUANTUM)}",
            f"- P50 latency: {_nearest_rank([case.latency_ms for case in report.cases], 50)} ms",
            f"- P95 latency: {_nearest_rank([case.latency_ms for case in report.cases], 95)} ms",
            "- Status counts: "
            + ", ".join(f"{status}={status_counts[status]}" for status in _STATUSES),
            "",
            (
                "| Case | Status | Score | Latency (ms) | Input tokens | Output tokens | "
                "Cost (CNY) | Error type |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for case in report.cases:
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_markdown(case.case_id),
                    _escape_markdown(case.status),
                    _format_decimal(case.score, _RATE_QUANTUM),
                    str(case.latency_ms),
                    str(case.input_tokens),
                    str(case.output_tokens),
                    _format_decimal(case.estimated_cost_cny, _COST_QUANTUM),
                    _escape_markdown(case.error_type or ""),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _validated_report(report: BaselineRunReport) -> BaselineRunReport:
    """Defend publication against unsafe values introduced through model_copy()."""
    try:
        return BaselineRunReport.model_validate(report.model_dump())
    except ValidationError as error:
        raise ValueError("report contains unsafe immutable-report metadata") from error


def _clean_owned_staging(staging_dir: Path) -> None:
    if staging_dir.exists():
        shutil.rmtree(staging_dir)


def _collision_error() -> FileExistsError:
    return FileExistsError("baseline report already exists")


def _raise_publish_error(error_number: int) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise _collision_error() from None
    raise OSError("atomic baseline report publication failed") from None


def _publish_darwin_no_replace(staging_dir: Path, final_dir: Path) -> None:
    """Use Darwin's documented `renamex_np(..., RENAME_EXCL)` primitive."""
    import ctypes

    try:
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
    except AttributeError:
        raise OSError("atomic baseline report publication unavailable") from None
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex_np.restype = ctypes.c_int
    ctypes.set_errno(0)
    # Xcode macOS SDK sys/stdio.h defines RENAME_EXCL as 0x00000004.
    result = renamex_np(os.fsencode(staging_dir), os.fsencode(final_dir), 0x00000004)
    if result != 0:
        _raise_publish_error(ctypes.get_errno())


def _publish_linux_no_replace(staging_dir: Path, final_dir: Path) -> None:
    """Use Linux renameat2's documented RENAME_NOREPLACE primitive."""
    import ctypes

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError("atomic baseline report publication unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    # Linux fs.h defines AT_FDCWD=-100 and RENAME_NOREPLACE=1.
    result = renameat2(-100, os.fsencode(staging_dir), -100, os.fsencode(final_dir), 1)
    if result != 0:
        _raise_publish_error(ctypes.get_errno())


def _publish_windows_no_replace(staging_dir: Path, final_dir: Path) -> None:
    """Move same-volume directories without MOVEFILE_REPLACE_EXISTING on Windows."""
    import ctypes

    win_dll: Any = getattr(ctypes, "WinDLL", None)
    set_last_error: Any = getattr(ctypes, "set_last_error", None)
    get_last_error: Any = getattr(ctypes, "get_last_error", None)
    if win_dll is None or set_last_error is None or get_last_error is None:
        raise OSError("atomic baseline report publication unavailable")
    move_file_ex = win_dll("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    move_file_ex.restype = ctypes.c_int
    set_last_error(0)
    if not move_file_ex(str(staging_dir), str(final_dir), 0):
        error_number = get_last_error()
        if error_number in {80, 183}:  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            raise _collision_error() from None
        raise OSError("atomic baseline report publication failed") from None


def _publish_staged_directory(staging_dir: Path, final_dir: Path) -> None:
    """Atomically make a completed report visible, refusing every existing target type."""
    system = platform.system()
    if system == "Darwin":
        _publish_darwin_no_replace(staging_dir, final_dir)
    elif system == "Linux":
        _publish_linux_no_replace(staging_dir, final_dir)
    elif system == "Windows":
        _publish_windows_no_replace(staging_dir, final_dir)
    else:
        raise OSError("atomic baseline report publication unavailable")


def write_baseline_report(
    report: BaselineRunReport,
    output_root: str | Path,
    *,
    generated_at: datetime,
) -> Path:
    """Atomically publish a complete, never-overwritten report directory."""
    report = _validated_report(report)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    timestamp = generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(output_root)
    mode_dir = root / report.mode
    final_dir = mode_dir / f"{timestamp}-{report.run_id}"
    if final_dir.parent != mode_dir or final_dir.name != f"{timestamp}-{report.run_id}":
        raise ValueError("report path must be safe and fixed")
    mode_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = mode_dir / f".{final_dir.name}-staging-{uuid4().hex}"
    try:
        staging_dir.mkdir()
        cases_dir = staging_dir / "cases"
        cases_dir.mkdir()
        _write_text(staging_dir / "report.json", _serialise(report))
        _write_text(staging_dir / "report.md", _render_markdown(report))
        for case in report.cases:
            _write_text(cases_dir / f"{case.case_id}.json", _serialise(case))
        _publish_staged_directory(staging_dir, final_dir)
    except Exception:
        _clean_owned_staging(staging_dir)
        raise
    return final_dir
