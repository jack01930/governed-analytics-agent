from __future__ import annotations

import json
from pathlib import Path

from governed_analytics.evals.suites import load_active_suites
from governed_analytics.evals.week2_runner import _suite_manifest_sha256

EXPECTED_WEEK2_SUITE_SHA256 = "ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_week2_protocol_remains_byte_frozen() -> None:
    cases = load_active_suites()

    assert tuple(case.case_id for case in cases) == (
        *(f"C2{index:02d}" for index in range(1, 21)),
        *(f"P2{index:02d}" for index in range(1, 21)),
        *(f"B2{index:02d}" for index in range(1, 11)),
        *(f"S2{index:02d}" for index in range(1, 21)),
    )
    assert _suite_manifest_sha256(cases) == EXPECTED_WEEK2_SUITE_SHA256


def test_archived_week2_evidence_keeps_the_frozen_suite_hash() -> None:
    report = json.loads(
        (_REPOSITORY_ROOT / "docs/reports/evidence/week2-live-v1/report.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (_REPOSITORY_ROOT / "docs/reports/evidence/manifest.json").read_text(encoding="utf-8")
    )

    assert report["suite_manifest_sha256"] == EXPECTED_WEEK2_SUITE_SHA256
    archived = next(item for item in manifest["runs"] if item["label"] == "week2-live-v1")
    assert archived["suite_manifest_sha256"] == EXPECTED_WEEK2_SUITE_SHA256
