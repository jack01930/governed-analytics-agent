"""Immutable JSON and Markdown baseline-report publication."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from governed_analytics.evals.models import BaselineCaseResult, BaselineRunReport

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
) -> BaselineRunReport:
    """Build exact aggregate metrics from one complete set of case outcomes."""
    case_results = tuple(cases)
    if not case_results:
        raise ValueError("baseline reports require at least one case")
    case_ids = tuple(case.case_id for case in case_results)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("baseline reports cannot contain duplicate case IDs")
    total_cases = Decimal(len(case_results))
    return BaselineRunReport(
        run_id=run_id,
        mode=mode,
        dataset_id=dataset_id,
        model=model,
        prompt_version=prompt_version,
        requested_model=requested_model,
        resolved_models=resolved_models,
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
    if final_dir.exists():
        raise FileExistsError(f"baseline report already exists: {final_dir}")
    staging_dir = mode_dir / f".{final_dir.name}-staging-{uuid4().hex}"
    try:
        staging_dir.mkdir()
        cases_dir = staging_dir / "cases"
        cases_dir.mkdir()
        _write_text(staging_dir / "report.json", _serialise(report))
        _write_text(staging_dir / "report.md", _render_markdown(report))
        for case in report.cases:
            _write_text(cases_dir / f"{case.case_id}.json", _serialise(case))
        if final_dir.exists():
            raise FileExistsError(f"baseline report already exists: {final_dir}")
        staging_dir.rename(final_dir)
    except Exception:
        _clean_owned_staging(staging_dir)
        raise
    return final_dir
