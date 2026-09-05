from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from governed_analytics.evals.week3 import reporting
from governed_analytics.evals.week3.models import Week3CaseResult, Week3RunReport
from governed_analytics.evals.week3.reporting import (
    cancel_week3_report_reservation,
    reserve_week3_report,
    write_week3_report,
)


def _report(run_id: str) -> Week3RunReport:
    case = Week3CaseResult(
        case_id="W3K001",
        cohort="known",
        suite="behavior",
        passed=True,
        first_candidate_conformant=None,
        final_conformant=True,
        behavior_conformant=True,
        tools_conformant=True,
        evidence_conformant=None,
        budget_conformant=True,
    )
    return Week3RunReport(
        mode="fixture",
        overall_manifest_sha256="a" * 64,
        known_cohort_sha256="b" * 64,
        heldout_cohort_sha256="c" * 64,
        run_id=run_id,
        cases=(case,),
    )


def test_report_reservation_rejects_duplicate_run_directory(tmp_path: Path) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="duplicate",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    try:
        with pytest.raises(FileExistsError):
            reserve_week3_report(
                tmp_path,
                mode="fixture",
                run_id="duplicate",
                timestamp=datetime(2026, 9, 4, tzinfo=UTC),
            )
    finally:
        cancel_week3_report_reservation(reservation)


def test_report_is_atomic_and_historical_output_is_never_overwritten(tmp_path: Path) -> None:
    timestamp = datetime(2026, 9, 4, tzinfo=UTC)
    reservation = reserve_week3_report(
        tmp_path, mode="fixture", run_id="historical", timestamp=timestamp
    )
    published = write_week3_report(reservation, _report("historical"))
    original = published.report_json.read_bytes()

    with pytest.raises(FileExistsError):
        reserve_week3_report(tmp_path, mode="fixture", run_id="historical", timestamp=timestamp)

    assert published.report_json.read_bytes() == original
    assert not tuple((tmp_path / "fixture").glob(".*-staging-reservation"))


def test_every_published_file_excludes_sensitive_payload_shapes(tmp_path: Path) -> None:
    sentinel = "ROW_SENTINEL_SELECT_SK_ENDPOINT"
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="sanitized",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )
    published = write_week3_report(reservation, _report("sanitized"))

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in published.report_dir.rglob("*")
        if path.is_file()
    ).casefold()
    assert sentinel.casefold() not in combined
    assert "select " not in combined
    assert "sk-" not in combined
    assert "endpoint" not in combined


def test_report_publish_failure_cleans_only_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = reserve_week3_report(
        tmp_path,
        mode="fixture",
        run_id="write-failure",
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
    )

    def fail_write(_path: Path, _contents: str) -> None:
        raise OSError("sensitive provider diagnostic")

    monkeypatch.setattr(reporting, "_write_text", fail_write)
    with pytest.raises(OSError):
        write_week3_report(reservation, _report("write-failure"))

    assert not reservation.staging_dir.exists()
    assert not reservation.final_dir.exists()
