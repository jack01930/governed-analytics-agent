"""Contract tests for aggregate baseline reports and immutable publication."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from governed_analytics.evals.models import BaselineCaseResult, BaselineRunReport
from governed_analytics.evals.reporting import build_baseline_run_report, write_baseline_report


def _case(
    case_id: str,
    *,
    status: Literal["passed", "wrong_answer", "invalid_sql", "execution_error"] = "passed",
    score: Decimal = Decimal("1"),
    latency_ms: int = 10,
    cost: Decimal = Decimal("0.1234567"),
    error_type: str | None = None,
) -> BaselineCaseResult:
    return BaselineCaseResult(
        case_id=case_id,
        generated_sql="select 1 as harmless",
        status=status,
        score=score,
        latency_ms=latency_ms,
        input_tokens=3,
        output_tokens=5,
        estimated_cost_cny=cost,
        error_type=error_type,
    )


def _report(
    cases: tuple[BaselineCaseResult, ...], *, mode: Literal["fixture", "live"] = "fixture"
) -> BaselineRunReport:
    return build_baseline_run_report(
        cases,
        run_id="run_01",
        mode=mode,
        dataset_id="a" * 64,
        model="qwen3.7-plus",
        prompt_version="baseline-v1",
        requested_model="qwen3.7-plus",
        resolved_models=("fixture-model",) if mode == "fixture" else ("qwen3.7-plus-2026-05-26",),
    )


def test_builder_calculates_exact_rates_and_rejects_empty_or_duplicate_cases() -> None:
    report = _report(
        (
            _case("G001"),
            _case("G002", status="wrong_answer", score=Decimal("0.5")),
            _case("G003", status="invalid_sql", score=Decimal("0"), error_type="sql_rejected"),
            _case(
                "G004", status="execution_error", score=Decimal("0"), error_type="database_error"
            ),
        )
    )
    assert report.result_accuracy == Decimal("0.375")
    assert report.valid_sql_rate == Decimal("0.75")
    assert report.execution_success_rate == Decimal("0.5")
    assert report.total_cost_cny == Decimal("0.4938268")

    with pytest.raises(ValueError, match="at least one"):
        _report(())
    with pytest.raises(ValueError, match="duplicate"):
        _report((_case("G001"), _case("G001")))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "passed", "score": Decimal("0.9")},
        {"status": "wrong_answer", "score": Decimal("1")},
        {"status": "invalid_sql", "score": Decimal("0"), "error_type": None},
        {"status": "execution_error", "score": Decimal("0.1"), "error_type": "database_error"},
        {"status": "passed", "score": Decimal("1"), "error_type": "should_not_appear"},
        {"status": "invalid_sql", "score": Decimal("0"), "error_type": "raw exception text"},
    ],
)
def test_case_result_status_and_error_type_invariants(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BaselineCaseResult.model_validate(
            {
                "case_id": "G001",
                "generated_sql": "select 1",
                "latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_cny": "0",
                **kwargs,
            }
        )


def test_report_model_enforces_safe_identifiers_and_model_contract() -> None:
    case = _case("G001")
    common: dict[str, Any] = dict(
        mode="fixture",
        dataset_id="a" * 64,
        model="qwen3.7-plus",
        prompt_version="v1",
        requested_model="qwen3.7-plus",
        resolved_models=(),
        result_accuracy=Decimal("1"),
        valid_sql_rate=Decimal("1"),
        execution_success_rate=Decimal("1"),
        total_cost_cny=Decimal("0"),
        cases=(case,),
    )
    safe_report = BaselineRunReport.model_validate({"run_id": "ok_20260902", **common})
    assert safe_report.resolved_models == ()
    for field, value in (("run_id", "../unsafe"), ("dataset_id", "A" * 64), ("model", "other")):
        payload = {"run_id": "ok", **common, field: value}
        with pytest.raises(ValidationError):
            BaselineRunReport.model_validate(payload)
    with pytest.raises(ValidationError, match="sorted"):
        BaselineRunReport.model_validate({"run_id": "ok", **common, "resolved_models": ("z", "a")})


def test_write_report_publishes_all_cases_round_trips_and_renders_fixture_label(
    tmp_path: Path,
) -> None:
    cases = tuple(
        _case(f"G{number:03d}", latency_ms=number, cost=Decimal("0.1")) for number in range(1, 21)
    )
    report = _report(cases)
    written = write_baseline_report(
        report, tmp_path, generated_at=datetime(2026, 9, 2, 3, 4, 5, tzinfo=UTC)
    )

    assert written == tmp_path / "fixture" / "20260902T030405Z-run_01"
    assert {path.name for path in (written / "cases").iterdir()} == {
        f"G{i:03d}.json" for i in range(1, 21)
    }
    loaded = BaselineRunReport.model_validate_json(
        (written / "report.json").read_text(encoding="utf-8")
    )
    assert loaded == report
    assert (
        json.loads((written / "cases" / "G001.json").read_text(encoding="utf-8"))[
            "estimated_cost_cny"
        ]
        == "0.1"
    )
    markdown = (written / "report.md").read_text(encoding="utf-8")
    assert "Harness validation, not model quality." in markdown
    assert "P50 latency: 10 ms" in markdown
    assert "P95 latency: 19 ms" in markdown
    assert markdown.count("| G") == 20
    assert "select 1 as harmless" not in markdown


def test_live_report_omits_fixture_label_and_escapes_markdown_cells(tmp_path: Path) -> None:
    report = _report((_case("G001"),), mode="live").model_copy(
        update={"prompt_version": "v1|unsafe"}
    )
    written = write_baseline_report(report, tmp_path, generated_at=datetime(2026, 9, 2, tzinfo=UTC))
    markdown = (written / "report.md").read_text(encoding="utf-8")
    assert "Harness validation, not model quality." not in markdown
    assert "v1\\|unsafe" in markdown


def test_collision_does_not_overwrite_prior_run_and_unsafe_run_id_cannot_write(
    tmp_path: Path,
) -> None:
    report = _report((_case("G001"),))
    stamp = datetime(2026, 9, 2, tzinfo=UTC)
    prior = write_baseline_report(report, tmp_path, generated_at=stamp)
    before = (prior / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        write_baseline_report(report, tmp_path, generated_at=stamp)
    assert (prior / "report.json").read_bytes() == before

    unsafe = report.model_copy(update={"run_id": "../escape"})
    with pytest.raises(ValueError, match="safe"):
        write_baseline_report(unsafe, tmp_path, generated_at=stamp)
    assert not (tmp_path / "escape").exists()


def test_write_failure_cleans_only_its_staging_and_preserves_previous_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report((_case("G001"),))
    prior = write_baseline_report(report, tmp_path, generated_at=datetime(2026, 9, 1, tzinfo=UTC))
    original = (prior / "report.json").read_bytes()

    import governed_analytics.evals.reporting as reporting

    original_write = reporting._write_text
    calls = 0

    def fail_on_second_write(path: Path, contents: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        original_write(path, contents)

    monkeypatch.setattr(reporting, "_write_text", fail_on_second_write)
    with pytest.raises(OSError, match="injected"):
        write_baseline_report(report, tmp_path, generated_at=datetime(2026, 9, 2, tzinfo=UTC))

    assert (prior / "report.json").read_bytes() == original
    assert not list((tmp_path / "fixture").glob(".*-staging-*"))
