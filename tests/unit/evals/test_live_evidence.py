from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from governed_analytics.evals.week2_models import Week2RunReport

EVIDENCE_ROOT = Path("docs/reports/evidence")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def test_versioned_live_evidence_is_complete_and_matches_manifest() -> None:
    manifest = json.loads((EVIDENCE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "governed-analytics-live-evidence-manifest-v2"
    assert [run["label"] for run in manifest["runs"]] == [
        "v1",
        "v2",
        "week2-live-v1",
    ]
    for run in manifest["runs"]:
        for item in run["files"]:
            archived = EVIDENCE_ROOT / item["path"]
            assert archived.is_file()
            assert archived.stat().st_size == item["bytes"]
            assert _sha256(archived) == item["sha256"]
        report_path = EVIDENCE_ROOT / run["label"] / "report.json"
        report = json.loads(report_path.read_text("utf-8"))
        assert report["run_id"] == run["run_id"]
        assert report["dataset_id"] == run["dataset_id"]
        assert report["prompt_version"] == run["prompt_version"]
        if run["label"] == "week2-live-v1":
            Week2RunReport.model_validate_json(report_path.read_text("utf-8"), strict=True)
            assert [case["case_id"] for case in report["cases"]] == [
                *[f"C{number:03d}" for number in range(201, 221)],
                *[f"P{number:03d}" for number in range(201, 221)],
                *[f"B{number:03d}" for number in range(201, 211)],
                *[f"S{number:03d}" for number in range(201, 221)],
            ]
            assert report["protocol_version"] == run["protocol_version"]
            assert report["suite_manifest_sha256"] == run["suite_manifest_sha256"]
            assert report["implementation_sha256"] == run["implementation_sha256"]
        else:
            assert [case["case_id"] for case in report["cases"]] == [
                f"G{number:03d}" for number in range(1, 21)
            ]

    for item in manifest["derived_files"]:
        archived = EVIDENCE_ROOT / item["path"]
        assert archived.stat().st_size == item["bytes"]
        assert _sha256(archived) == item["sha256"]


def test_versioned_live_evidence_contains_no_credential_like_text() -> None:
    credential_pattern = re.compile(r"sk-[A-Za-z0-9_-]{16,}")

    for path in EVIDENCE_ROOT.rglob("*"):
        if path.is_file():
            assert credential_pattern.search(path.read_text(encoding="utf-8")) is None
