from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from governed_analytics.evals.pricing import load_model_pricing
from governed_analytics.evals.week2_models import (
    ComparisonReference,
    SuiteName,
    Week2CaseResult,
    Week2RunReport,
)
from governed_analytics.evals.week2_reporting import (
    build_week2_run_report,
    pricing_snapshot_sha256,
    reserve_week2_report,
    write_week2_report,
)


def _case_ids() -> tuple[tuple[str, SuiteName], ...]:
    return tuple(
        [(f"C2{number:02d}", "core-v2") for number in range(1, 21)]
        + [(f"P2{number:02d}", "paraphrase") for number in range(1, 21)]
        + [(f"B2{number:02d}", "boundary") for number in range(1, 11)]
        + [(f"S2{number:02d}", "safety") for number in range(1, 21)]
    )


def _perfect_cases() -> tuple[Week2CaseResult, ...]:
    cases: list[Week2CaseResult] = []
    for case_id, suite in _case_ids():
        if suite == "safety":
            cases.append(
                Week2CaseResult(
                    case_id=case_id,
                    suite=suite,
                    status="rejected",
                    expected_rejection="not_readonly_query",
                    observed_rejection="not_readonly_query",
                )
            )
        else:
            cases.append(
                Week2CaseResult(
                    case_id=case_id,
                    suite=suite,
                    status="passed",
                    result_score=Decimal("1"),
                    output_contract_conformant=True,
                    query_id="a" * 64,
                )
            )
    return tuple(cases)


def _comparison() -> ComparisonReference:
    return ComparisonReference(
        reference_run_id="65c87bf0355047a9b23243f70756e3f4",
        reference_protocol="baseline-system-v2",
        reference_dataset_id="b" * 64,
        reference_report_sha256="e" * 64,
        comparison_status="not_comparable",
    )


def _report(
    cases: tuple[Week2CaseResult, ...] | None = None,
    *,
    mode: Literal["fixture", "live"] = "fixture",
) -> Week2RunReport:
    requested_model = "fixture-oracle" if mode == "fixture" else "deepseek-v4-flash"
    pricing = (
        load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
        if mode == "live"
        else None
    )
    return build_week2_run_report(
        cases or _perfect_cases(),
        run_id="week2-unit",
        mode=mode,
        suite_manifest_sha256="c" * 64,
        implementation_sha256="d" * 64,
        dataset_id="b" * 64,
        prompt_version="baseline-system-v2+week2-suite-v1",
        requested_model=requested_model,
        resolved_models=(requested_model,),
        comparison_reference=_comparison(),
        pricing=pricing,
    )


def test_week2_report_separates_business_contract_and_safety_denominators() -> None:
    cases = list(_perfect_cases())
    cases[0] = Week2CaseResult(
        case_id="C201",
        suite="core-v2",
        status="contract_violation",
        result_score=Decimal("1"),
        output_contract_conformant=False,
        query_id="d" * 64,
        finish_reason="length",
        output_truncated=True,
        input_tokens=11,
        output_tokens=12,
        latency_ms=13,
        estimated_cost_cny=Decimal("0.01"),
    )
    cases[-1] = Week2CaseResult(
        case_id="S220",
        suite="safety",
        status="unexpected_accept",
        error_type="safety_unexpected_accept",
        expected_rejection="select_star",
    )

    report = _report(tuple(cases), mode="live")

    assert report.result_accuracy == Decimal("1")
    assert report.output_contract_rate == Decimal("49") / Decimal("50")
    assert report.safety_rejection_rate == Decimal("19") / Decimal("20")
    assert report.truncated_generation_count == 1
    assert report.total_input_tokens == 11
    assert report.total_output_tokens == 12
    assert report.total_cost_cny == Decimal("0.01")
    assert report.cost_estimate_complete
    assert report.unpriced_call_count == 0
    assert report.suite_summaries[0].result_accuracy == Decimal("1")
    assert report.suite_summaries[0].output_contract_rate == Decimal("19") / Decimal("20")
    assert report.suite_summaries[-1].result_accuracy is None


def test_week2_case_rejects_inconsistent_or_unsafe_telemetry_contracts() -> None:
    with pytest.raises(ValidationError, match="output_truncated"):
        Week2CaseResult(
            case_id="C201",
            suite="core-v2",
            status="passed",
            result_score=Decimal("1"),
            output_contract_conformant=True,
            finish_reason="length",
        )
    with pytest.raises(ValidationError, match="safety cases cannot"):
        Week2CaseResult(
            case_id="S201",
            suite="safety",
            status="rejected",
            expected_rejection="not_readonly_query",
            observed_rejection="not_readonly_query",
            input_tokens=1,
        )
    with pytest.raises(ValidationError, match="unpriced calls cannot"):
        Week2CaseResult(
            case_id="C201",
            suite="core-v2",
            status="invalid_sql",
            estimated_cost_cny=Decimal("0.01"),
            error_type="pricing_failed",
        )


def test_week2_report_publication_is_atomic_non_overwriting_and_contains_no_sql(
    tmp_path: Path,
) -> None:
    report = _report()
    generated_at = datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC)

    output = write_week2_report(report, tmp_path, generated_at=generated_at)

    assert output.name == "20260904T010203Z-week2-unit"
    assert len(tuple((output / "cases").glob("*.json"))) == 70
    payload = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert payload["result_accuracy"] == "1"
    assert payload["safety_rejection_rate"] == "1"
    assert payload["implementation_sha256"] == "d" * 64
    assert payload["comparison_reference"]["reference_report_sha256"] == "e" * 64
    assert payload["pricing_requested_model"] is None
    assert payload["pricing_resolved_model"] is None
    assert payload["pricing_snapshot_sha256"] is None
    assert payload["cost_estimate_complete"] is True
    assert payload["unpriced_call_count"] == 0
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert f"- Implementation SHA-256: {'d' * 64}" in markdown
    assert f"- Reference report SHA-256: {'e' * 64}" in markdown
    assert "- Pricing requested model: (none)" in markdown
    assert "- Pricing resolved model: (none)" in markdown
    assert "- Pricing snapshot SHA-256: (none)" in markdown
    assert "- Known estimated cost (CNY): 0.000000" in markdown
    assert "- Cost estimate complete: true" in markdown
    assert "- Unpriced call count: 0" in markdown
    all_text = "\n".join(path.read_text("utf-8") for path in output.rglob("*.*"))
    assert "select " not in all_text.lower()
    assert "api_key" not in all_text.lower()

    with pytest.raises(FileExistsError):
        write_week2_report(report, tmp_path, generated_at=generated_at)


def test_live_week2_report_publishes_pricing_model_binding(tmp_path: Path) -> None:
    report = _report(mode="live")
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")

    output = write_week2_report(
        report,
        tmp_path,
        generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
    )

    payload = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert payload["requested_model"] == "deepseek-v4-flash"
    assert payload["resolved_models"] == ["deepseek-v4-flash"]
    assert payload["pricing_requested_model"] == "deepseek-v4-flash"
    assert payload["pricing_resolved_model"] == "DeepSeek-V4-Flash-0731"
    assert payload["pricing_snapshot_sha256"] == pricing_snapshot_sha256(pricing)
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "- Pricing requested model: deepseek-v4-flash" in markdown
    assert "- Pricing resolved model: DeepSeek-V4-Flash-0731" in markdown
    assert f"- Pricing snapshot SHA-256: {pricing_snapshot_sha256(pricing)}" in markdown


def test_unpriced_calls_make_the_known_cost_incomplete_and_are_derived(
    tmp_path: Path,
) -> None:
    cases = list(_perfect_cases())
    cases[0] = Week2CaseResult(
        case_id="C201",
        suite="core-v2",
        status="invalid_sql",
        input_tokens=11,
        output_tokens=12,
        latency_ms=13,
        error_type="pricing_failed",
    )

    report = _report(tuple(cases), mode="live")
    output = write_week2_report(
        report,
        tmp_path,
        generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
    )

    assert not report.cost_estimate_complete
    assert report.unpriced_call_count == 1
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "- Known estimated cost (CNY): 0.000000" in markdown
    assert "- Cost estimate complete: false" in markdown
    assert "- Unpriced call count: 1" in markdown

    for update in (
        {"cost_estimate_complete": True},
        {"unpriced_call_count": 0},
    ):
        tampered = report.model_copy(update=update)
        with pytest.raises(ValueError, match="unsafe immutable-report metadata"):
            write_week2_report(
                tampered,
                tmp_path / next(iter(update)),
                generated_at=datetime(2026, 9, 4, 1, 2, 4, tzinfo=UTC),
            )


def test_live_week2_report_rejects_pricing_for_a_different_requested_model() -> None:
    report = _report(mode="live")

    with pytest.raises(ValidationError, match="pricing must match"):
        Week2RunReport.model_validate(
            report.model_dump() | {"pricing_requested_model": "different-model"}
        )


def test_fixture_report_enforces_identity_zero_telemetry_and_comparison_boundary() -> None:
    report = _report()

    assert report.requested_model == "fixture-oracle"
    assert report.resolved_models == ("fixture-oracle",)
    assert report.cost_estimate_complete
    assert report.unpriced_call_count == 0
    assert report.comparison_reference.comparison_status == "not_comparable"

    for update, message in (
        (
            {"requested_model": "other", "resolved_models": ("other",)},
            "fixed fixture model identity",
        ),
        (
            {
                "comparison_reference": report.comparison_reference.model_copy(
                    update={"comparison_status": "comparable"}
                )
            },
            "cannot be historically comparable",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            Week2RunReport.model_validate(report.model_dump() | update)

    cases = list(_perfect_cases())
    cases[0] = cases[0].model_copy(update={"input_tokens": 1})
    with pytest.raises(ValidationError, match="live-call telemetry"):
        _report(tuple(cases))


def test_live_report_rejects_fixture_model_identity() -> None:
    report = _report(mode="live")

    with pytest.raises(ValidationError, match="fixture model identities"):
        Week2RunReport.model_validate(
            report.model_dump()
            | {
                "requested_model": "fixture-oracle",
                "resolved_models": ("fixture-oracle",),
                "pricing_requested_model": "fixture-oracle",
            }
        )


def test_week2_report_refuses_tampered_aggregates_before_publication(
    tmp_path: Path,
) -> None:
    report = _report().model_copy(update={"result_accuracy": Decimal("0")})

    with pytest.raises(ValueError, match="unsafe immutable-report metadata"):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert not tuple(tmp_path.rglob("report.json"))


def test_week2_report_refuses_an_invalid_implementation_digest(tmp_path: Path) -> None:
    report = _report().model_copy(update={"implementation_sha256": "not-a-sha256"})

    with pytest.raises(ValueError, match="unsafe immutable-report metadata"):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert not tuple(tmp_path.rglob("report.json"))


def test_week2_report_refuses_an_invalid_reference_report_digest(tmp_path: Path) -> None:
    report = _report()
    report = report.model_copy(
        update={
            "comparison_reference": report.comparison_reference.model_copy(
                update={"reference_report_sha256": "not-a-sha256"}
            )
        }
    )

    with pytest.raises(ValueError, match="unsafe immutable-report metadata"):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert not tuple(tmp_path.rglob("report.json"))


def test_week2_report_write_failure_removes_its_staging_without_a_half_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report()
    target = tmp_path / "fixture" / "20260904T010203Z-week2-unit"
    calls = 0

    def fail_on_second_write(path: Path, contents: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        path.write_text(contents, encoding="utf-8", newline="\n")

    monkeypatch.setattr(
        "governed_analytics.evals.week2_reporting._write_text", fail_on_second_write
    )

    with pytest.raises(OSError, match="injected write failure"):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert not target.exists()
    assert not list(target.parent.glob(f".{target.name}-staging-*"))
    assert not tuple(tmp_path.rglob("report.json"))


@pytest.mark.parametrize("existing_target", ("file", "directory", "symlink"))
def test_week2_report_never_overwrites_an_existing_target(
    tmp_path: Path, existing_target: str
) -> None:
    report = _report()
    target = tmp_path / "fixture" / "20260904T010203Z-week2-unit"
    target.parent.mkdir()
    sentinel = "prior-report-must-survive"
    if existing_target == "file":
        target.write_text(sentinel, encoding="utf-8")
    elif existing_target == "directory":
        target.mkdir()
        (target / "sentinel").write_text(sentinel, encoding="utf-8")
    else:
        prior_target = tmp_path / "prior-target"
        prior_target.mkdir()
        (prior_target / "sentinel").write_text(sentinel, encoding="utf-8")
        target.symlink_to(prior_target, target_is_directory=True)

    with pytest.raises(FileExistsError):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert target.exists() or target.is_symlink()
    if existing_target == "file":
        assert target.read_text(encoding="utf-8") == sentinel
    elif existing_target == "directory":
        assert (target / "sentinel").read_text(encoding="utf-8") == sentinel
    else:
        assert (tmp_path / "prior-target" / "sentinel").read_text(encoding="utf-8") == sentinel
    assert not list(target.parent.glob(f".{target.name}-staging-*"))


def test_week2_report_reservation_is_exclusive_and_consumed_by_publication(
    tmp_path: Path,
) -> None:
    report = _report()
    generated_at = datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC)
    reservation = reserve_week2_report(
        tmp_path,
        mode=report.mode,
        run_id=report.run_id,
        generated_at=generated_at,
    )

    assert reservation.active
    assert reservation.staging_dir.is_dir()
    with pytest.raises(FileExistsError, match="already exists or is reserved"):
        reserve_week2_report(
            tmp_path,
            mode=report.mode,
            run_id=report.run_id,
            generated_at=generated_at,
        )

    output = write_week2_report(
        report,
        generated_at=generated_at,
        reservation=reservation,
    )

    assert output == reservation.final_dir
    assert output.is_dir()
    assert not reservation.active
    assert not reservation.staging_dir.exists()


def test_week2_report_reservation_cleans_up_when_preflight_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedInterruption(BaseException):
        pass

    def interrupt_probe(_mode_dir: Path) -> None:
        raise InjectedInterruption

    monkeypatch.setattr(
        "governed_analytics.evals.week2_reporting._probe_atomic_publication",
        interrupt_probe,
    )

    with pytest.raises(InjectedInterruption):
        reserve_week2_report(
            tmp_path,
            mode="fixture",
            run_id="interrupted-preflight",
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert not tuple(tmp_path.rglob("*staging-reservation*"))


@pytest.mark.parametrize("identity_mismatch", ("mode", "run_id", "timestamp"))
def test_week2_report_refuses_and_cleans_a_mismatched_reservation(
    tmp_path: Path, identity_mismatch: str
) -> None:
    generated_at = datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC)
    report = _report()
    reservation = reserve_week2_report(
        tmp_path,
        mode=report.mode,
        run_id=report.run_id,
        generated_at=generated_at,
    )
    if identity_mismatch == "mode":
        report = _report(mode="live")
    elif identity_mismatch == "run_id":
        report = report.model_copy(update={"run_id": "different-run"})
    else:
        generated_at = datetime(2026, 9, 4, 1, 2, 4, tzinfo=UTC)

    with pytest.raises(ValueError, match="reservation does not match"):
        write_week2_report(
            report,
            generated_at=generated_at,
            reservation=reservation,
        )

    assert not reservation.active
    assert not reservation.staging_dir.exists()


def test_week2_report_refuses_a_target_created_at_the_publication_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report()
    generated_at = datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC)
    target = tmp_path / "fixture" / "20260904T010203Z-week2-unit"
    reservation = reserve_week2_report(
        tmp_path,
        mode=report.mode,
        run_id=report.run_id,
        generated_at=generated_at,
    )

    def create_target_then_publish(staging_dir: Path, final_dir: Path) -> None:
        assert final_dir == target
        final_dir.mkdir()
        raise FileExistsError("injected publication race")

    monkeypatch.setattr(
        "governed_analytics.evals.week2_reporting._publish_staged_directory",
        create_target_then_publish,
    )

    with pytest.raises(FileExistsError):
        write_week2_report(
            report,
            generated_at=generated_at,
            reservation=reservation,
        )

    assert target.is_dir()
    assert not tuple(target.iterdir())
    assert not list(target.parent.glob(f".{target.name}-staging-*"))


def test_week2_report_rejects_a_unique_secret_marker_before_any_publication(tmp_path: Path) -> None:
    marker = "unique-secret-marker|must-not-be-published"
    report = _report().model_copy(update={"prompt_version": marker})

    with pytest.raises(ValueError, match="unsafe immutable-report metadata"):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    published_text = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()
    )
    assert marker not in published_text


def test_week2_report_rejects_secret_shaped_model_metadata(tmp_path: Path) -> None:
    marker = "sk-0123456789abcdef0123456789abcdef"
    report = _report().model_copy(update={"requested_model": marker})

    with pytest.raises(ValueError, match="unsafe immutable-report metadata"):
        write_week2_report(
            report,
            tmp_path,
            generated_at=datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC),
        )

    assert not tuple(tmp_path.rglob("report.json"))
