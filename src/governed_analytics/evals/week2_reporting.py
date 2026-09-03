"""Build and atomically publish sanitized Week 2 evaluation reports."""

from __future__ import annotations

import json
import re
import stat
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from governed_analytics.evals.models import BaselineRunReport
from governed_analytics.evals.pricing import ModelPricing
from governed_analytics.evals.reporting import (
    _clean_owned_staging,
    _publish_staged_directory,
    _write_text,
)
from governed_analytics.evals.week2_models import (
    ComparisonReference,
    Week2CaseResult,
    Week2RunReport,
    derive_week2_aggregates,
)

_RATE_QUANTUM = Decimal("0.0001")
_COST_QUANTUM = Decimal("0.000001")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class Week2ReportReservation:
    """Exclusive, preflighted target and staging directory for one report."""

    mode: Literal["fixture", "live"]
    run_id: str
    generated_at_utc: datetime
    final_dir: Path
    staging_dir: Path
    staging_device: int
    staging_inode: int
    _active: bool = field(default=True, init=False, repr=False, compare=False)

    @property
    def active(self) -> bool:
        return self._active


def pricing_snapshot_sha256(pricing: ModelPricing) -> str:
    """Hash every validated pricing field using canonical JSON."""
    canonical = json.dumps(
        pricing.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def reference_report_sha256(reference: BaselineRunReport) -> str:
    """Bind comparisons to every field of the strictly parsed Week 1 report."""
    canonical = json.dumps(
        reference.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def build_week2_run_report(
    cases: Sequence[Week2CaseResult],
    *,
    run_id: str,
    mode: Literal["fixture", "live"],
    suite_manifest_sha256: str,
    implementation_sha256: str,
    dataset_id: str,
    prompt_version: str,
    requested_model: str,
    resolved_models: tuple[str, ...],
    comparison_reference: ComparisonReference,
    pricing: ModelPricing | None = None,
) -> Week2RunReport:
    case_results = tuple(cases)
    derived = derive_week2_aggregates(case_results)
    return Week2RunReport(
        run_id=run_id,
        mode=mode,
        suite_manifest_sha256=suite_manifest_sha256,
        implementation_sha256=implementation_sha256,
        dataset_id=dataset_id,
        prompt_version=prompt_version,
        requested_model=requested_model,
        resolved_models=resolved_models,
        pricing_requested_model=(pricing.requested_model if pricing is not None else None),
        pricing_resolved_model=(pricing.resolved_model if pricing is not None else None),
        pricing_snapshot_sha256=(pricing_snapshot_sha256(pricing) if pricing is not None else None),
        pricing_effective_date=(pricing.effective_date if pricing is not None else None),
        pricing_basis=(pricing.pricing_basis if pricing is not None else None),
        comparison_reference=comparison_reference,
        result_accuracy=derived.result_accuracy,
        output_contract_rate=derived.output_contract_rate,
        valid_sql_rate=derived.valid_sql_rate,
        execution_success_rate=derived.execution_success_rate,
        safety_rejection_rate=derived.safety_rejection_rate,
        truncated_generation_count=derived.truncated_generation_count,
        total_input_tokens=derived.total_input_tokens,
        total_output_tokens=derived.total_output_tokens,
        total_cost_cny=derived.total_cost_cny,
        cost_estimate_complete=derived.cost_estimate_complete,
        unpriced_call_count=derived.unpriced_call_count,
        suite_summaries=derived.suite_summaries,
        cases=case_results,
    )


def _serialise(model: Week2RunReport | Week2CaseResult) -> str:
    return (
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )


def _format_decimal(value: Decimal, quantum: Decimal) -> str:
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _nearest_rank(latencies: Sequence[int], percentile: int) -> int:
    if not latencies:
        return 0
    ordered = sorted(latencies)
    rank = (len(ordered) * percentile + 99) // 100
    return ordered[rank - 1]


def _render_markdown(report: Week2RunReport) -> str:
    execute_cases = [case for case in report.cases if case.suite != "safety"]
    status_counts: Counter[str] = Counter(case.status for case in report.cases)
    result_accuracy = _format_decimal(report.result_accuracy, _RATE_QUANTUM)
    contract_rate = _format_decimal(report.output_contract_rate, _RATE_QUANTUM)
    valid_sql_rate = _format_decimal(report.valid_sql_rate, _RATE_QUANTUM)
    execution_rate = _format_decimal(report.execution_success_rate, _RATE_QUANTUM)
    safety_rate = _format_decimal(report.safety_rejection_rate, _RATE_QUANTUM)
    p50 = _nearest_rank([case.latency_ms for case in execute_cases], 50)
    p95 = _nearest_rank([case.latency_ms for case in execute_cases], 95)
    lines = ["# Week 2 evaluation report", ""]
    if report.mode == "fixture":
        lines.extend(["**Harness validation, not live model quality.**", ""])
    lines.extend(
        [
            f"- Run ID: {_escape(report.run_id)}",
            f"- Mode: {_escape(report.mode)}",
            f"- Dataset ID: {_escape(report.dataset_id)}",
            f"- Suite manifest: {_escape(report.suite_manifest_sha256)}",
            f"- Implementation SHA-256: {_escape(report.implementation_sha256)}",
            f"- Prompt version: {_escape(report.prompt_version)}",
            f"- Requested model: {_escape(report.requested_model)}",
            "- Resolved models: " + _escape(", ".join(report.resolved_models) or "(none)"),
            "- Pricing requested model: " + _escape(report.pricing_requested_model or "(none)"),
            "- Pricing resolved model: " + _escape(report.pricing_resolved_model or "(none)"),
            "- Pricing snapshot SHA-256: " + _escape(report.pricing_snapshot_sha256 or "(none)"),
            "- Pricing effective date: " + _escape(report.pricing_effective_date or "(none)"),
            "- Pricing basis: " + _escape(report.pricing_basis or "(none)"),
            f"- Result accuracy (50 execute cases): {result_accuracy}",
            f"- Output contract rate: {contract_rate}",
            f"- Valid SQL rate: {valid_sql_rate}",
            f"- Execution success rate: {execution_rate}",
            f"- Safety rejection rate (20 cases): {safety_rate}",
            f"- Truncated generations: {report.truncated_generation_count}",
            "- Total input/output tokens: "
            f"{report.total_input_tokens}/{report.total_output_tokens}",
            "- Known estimated cost (CNY): "
            + _format_decimal(report.total_cost_cny, _COST_QUANTUM),
            f"- Cost estimate complete: {str(report.cost_estimate_complete).lower()}",
            f"- Unpriced call count: {report.unpriced_call_count}",
            f"- P50/P95 latency: {p50}/{p95} ms",
            "- Comparison: " + _escape(report.comparison_reference.comparison_scope),
            f"- Comparison status: {_escape(report.comparison_reference.comparison_status)}",
            "- Reference report SHA-256: "
            + _escape(report.comparison_reference.reference_report_sha256),
            "- Status counts: "
            + ", ".join(f"{name}={status_counts[name]}" for name in sorted(status_counts)),
            "",
            "| Suite | Cases | Passed | Result accuracy | Contract rate | Safety rejection |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for summary in report.suite_summaries:
        lines.append(
            "| "
            + " | ".join(
                (
                    summary.suite,
                    str(summary.case_count),
                    str(summary.passed_count),
                    ""
                    if summary.result_accuracy is None
                    else _format_decimal(summary.result_accuracy, _RATE_QUANTUM),
                    ""
                    if summary.output_contract_rate is None
                    else _format_decimal(summary.output_contract_rate, _RATE_QUANTUM),
                    ""
                    if summary.safety_rejection_rate is None
                    else _format_decimal(summary.safety_rejection_rate, _RATE_QUANTUM),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| Case | Suite | Status | Result | Contract | Finish | Truncated | "
            "Query ID | Error | Expected rejection | Observed rejection |",
            "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in report.cases:
        lines.append(
            "| "
            + " | ".join(
                (
                    case.case_id,
                    case.suite,
                    case.status,
                    ""
                    if case.result_score is None
                    else _format_decimal(case.result_score, _RATE_QUANTUM),
                    ""
                    if case.output_contract_conformant is None
                    else str(case.output_contract_conformant).lower(),
                    case.finish_reason or "",
                    str(case.output_truncated).lower(),
                    case.query_id or "",
                    case.error_type or "",
                    case.expected_rejection or "",
                    case.observed_rejection or "",
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _normalise_generated_at(generated_at: datetime) -> datetime:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return generated_at.astimezone(UTC)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _remove_owned_path(path: Path, *, device: int, inode: int) -> None:
    """Remove only the exact file-system object created by this process."""
    try:
        current = path.lstat()
        if current.st_dev != device or current.st_ino != inode:
            return
        if stat.S_ISDIR(current.st_mode):
            _clean_owned_staging(path)
        elif stat.S_ISREG(current.st_mode):
            path.unlink()
    except OSError:
        pass


def cancel_week2_report_reservation(reservation: Week2ReportReservation) -> None:
    """Best-effort cleanup that cannot delete a replaced or symlinked path."""
    if not reservation.active:
        return
    _remove_owned_path(
        reservation.staging_dir,
        device=reservation.staging_device,
        inode=reservation.staging_inode,
    )
    object.__setattr__(reservation, "_active", False)


def _probe_atomic_publication(mode_dir: Path) -> None:
    token = uuid4().hex
    source = mode_dir / f".week2-atomic-probe-source-{token}"
    target = mode_dir / f".week2-atomic-probe-target-{token}"
    collision_source = mode_dir / f".week2-no-replace-source-{token}"
    collision_target = mode_dir / f".week2-no-replace-target-{token}"
    try:
        source.mkdir()
        source_stat = source.lstat()
        _publish_staged_directory(source, target)
        target_stat = target.lstat()
        if (
            _path_exists(source)
            or not stat.S_ISDIR(target_stat.st_mode)
            or target_stat.st_dev != source_stat.st_dev
            or target_stat.st_ino != source_stat.st_ino
        ):
            raise OSError("atomic Week 2 report publication unavailable")

        with collision_source.open("xb") as source_file:
            source_file.write(b"source")
        collision_source_stat = collision_source.lstat()
        with collision_target.open("xb") as target_file:
            target_file.write(b"sentinel")
        collision_target_stat = collision_target.lstat()
        try:
            _publish_staged_directory(collision_source, collision_target)
        except FileExistsError:
            pass
        else:
            raise OSError("atomic no-replace Week 2 report publication unavailable")
        preserved_source_stat = collision_source.lstat()
        preserved_target_stat = collision_target.lstat()
        if (
            preserved_source_stat.st_dev != collision_source_stat.st_dev
            or preserved_source_stat.st_ino != collision_source_stat.st_ino
            or preserved_target_stat.st_dev != collision_target_stat.st_dev
            or preserved_target_stat.st_ino != collision_target_stat.st_ino
            or collision_target.read_bytes() != b"sentinel"
        ):
            raise OSError("atomic no-replace Week 2 report publication unavailable")
    finally:
        if "source_stat" in locals():
            _remove_owned_path(
                source,
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
            )
            _remove_owned_path(
                target,
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
            )
        if "collision_source_stat" in locals():
            _remove_owned_path(
                collision_source,
                device=collision_source_stat.st_dev,
                inode=collision_source_stat.st_ino,
            )
            _remove_owned_path(
                collision_target,
                device=collision_source_stat.st_dev,
                inode=collision_source_stat.st_ino,
            )
        if "collision_target_stat" in locals():
            _remove_owned_path(
                collision_target,
                device=collision_target_stat.st_dev,
                inode=collision_target_stat.st_ino,
            )


def reserve_week2_report(
    output_root: str | Path,
    *,
    mode: Literal["fixture", "live"],
    run_id: str,
    generated_at: datetime,
) -> Week2ReportReservation:
    """Reserve and preflight the exact report target before model calls begin."""
    generated_at_utc = _normalise_generated_at(generated_at)
    if mode not in {"fixture", "live"} or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("report target metadata is invalid")
    timestamp = generated_at_utc.strftime("%Y%m%dT%H%M%SZ")
    mode_dir = Path(output_root).absolute() / mode
    final_name = f"{timestamp}-{run_id}"
    final_dir = mode_dir / final_name
    staging_dir = mode_dir / f".{final_name}-staging-reservation"
    if final_dir.parent != mode_dir or final_dir.name != final_name:
        raise ValueError("report path must be safe and fixed")

    staging_created = False
    try:
        mode_dir.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir()
        staging_created = True
        staging_stat = staging_dir.lstat()
    except FileExistsError:
        raise FileExistsError("Week 2 report already exists or is reserved") from None
    except BaseException as error:
        if staging_created:
            try:
                interrupted_stat = staging_dir.lstat()
            except OSError:
                pass
            else:
                _remove_owned_path(
                    staging_dir,
                    device=interrupted_stat.st_dev,
                    inode=interrupted_stat.st_ino,
                )
        if isinstance(error, Exception):
            raise OSError("Week 2 report publication unavailable") from None
        raise

    reservation = Week2ReportReservation(
        mode=mode,
        run_id=run_id,
        generated_at_utc=generated_at_utc,
        final_dir=final_dir,
        staging_dir=staging_dir,
        staging_device=staging_stat.st_dev,
        staging_inode=staging_stat.st_ino,
    )
    try:
        if _path_exists(final_dir):
            raise FileExistsError("Week 2 report already exists or is reserved")
        write_probe = staging_dir / ".write-probe"
        with write_probe.open("xb") as probe_file:
            probe_file.write(b"")
        write_probe.unlink()
        _probe_atomic_publication(mode_dir)
        if _path_exists(final_dir):
            raise FileExistsError("Week 2 report already exists or is reserved")
    except FileExistsError:
        cancel_week2_report_reservation(reservation)
        raise FileExistsError("Week 2 report already exists or is reserved") from None
    except BaseException as error:
        cancel_week2_report_reservation(reservation)
        if isinstance(error, Exception):
            raise OSError("Week 2 report publication unavailable") from None
        raise
    return reservation


def _validate_reservation(
    report: Week2RunReport,
    reservation: Week2ReportReservation,
    generated_at: datetime,
) -> None:
    generated_at_utc = _normalise_generated_at(generated_at)
    final_name = f"{generated_at_utc.strftime('%Y%m%dT%H%M%SZ')}-{report.run_id}"
    mode_dir = reservation.final_dir.parent
    if (
        not reservation.active
        or reservation.mode != report.mode
        or reservation.run_id != report.run_id
        or reservation.generated_at_utc != generated_at_utc
        or mode_dir.name != report.mode
        or reservation.final_dir != mode_dir / final_name
        or reservation.staging_dir != mode_dir / f".{final_name}-staging-reservation"
    ):
        raise ValueError("Week 2 report reservation does not match the report")
    if _path_exists(reservation.final_dir):
        raise FileExistsError("Week 2 report already exists or is reserved")
    try:
        staging_stat = reservation.staging_dir.lstat()
        if (
            not stat.S_ISDIR(staging_stat.st_mode)
            or staging_stat.st_dev != reservation.staging_device
            or staging_stat.st_ino != reservation.staging_inode
            or any(reservation.staging_dir.iterdir())
        ):
            raise OSError
    except Exception:
        raise OSError("Week 2 report reservation unavailable") from None


def write_week2_report(
    report: Week2RunReport,
    output_root: str | Path | None = None,
    *,
    generated_at: datetime,
    reservation: Week2ReportReservation | None = None,
) -> Path:
    """Publish JSON, Markdown, and per-case JSON without raw SQL or provider payloads."""
    try:
        report = Week2RunReport.model_validate(report.model_dump())
    except ValidationError as error:
        if reservation is not None:
            cancel_week2_report_reservation(reservation)
        raise ValueError("report contains unsafe immutable-report metadata") from error
    if reservation is not None and output_root is not None:
        cancel_week2_report_reservation(reservation)
        raise ValueError("reserved publication cannot override its output root")
    if reservation is None:
        if output_root is None:
            raise ValueError("report output root is required")
        reservation = reserve_week2_report(
            output_root,
            mode=report.mode,
            run_id=report.run_id,
            generated_at=generated_at,
        )
    try:
        _validate_reservation(report, reservation, generated_at)
        staging_dir = reservation.staging_dir
        cases_dir = staging_dir / "cases"
        cases_dir.mkdir()
        _write_text(staging_dir / "report.json", _serialise(report))
        _write_text(staging_dir / "report.md", _render_markdown(report))
        for case in report.cases:
            _write_text(cases_dir / f"{case.case_id}.json", _serialise(case))
        _publish_staged_directory(staging_dir, reservation.final_dir)
        final_stat = reservation.final_dir.lstat()
        if (
            _path_exists(staging_dir)
            or not stat.S_ISDIR(final_stat.st_mode)
            or final_stat.st_dev != reservation.staging_device
            or final_stat.st_ino != reservation.staging_inode
        ):
            raise OSError("atomic Week 2 report publication failed")
    except BaseException:
        cancel_week2_report_reservation(reservation)
        raise
    object.__setattr__(reservation, "_active", False)
    return reservation.final_dir
