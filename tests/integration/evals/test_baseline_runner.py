from __future__ import annotations

from pathlib import Path

import pytest

from governed_analytics.evals.runner import run_baseline


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fixture_baseline_executes_all_cases_without_cost(tmp_path: Path) -> None:
    report = await run_baseline(mode="fixture", output_root=tmp_path)

    assert len(report.cases) == 20
    assert report.result_accuracy == 1
    assert report.valid_sql_rate == 1
    assert report.execution_success_rate == 1
    assert report.total_cost_cny == 0
    assert all(case.status == "passed" for case in report.cases)
    report_dirs = list((tmp_path / "fixture").iterdir())
    assert len(report_dirs) == 1
    assert (report_dirs[0] / "report.json").is_file()
    assert len(list((report_dirs[0] / "cases").glob("G*.json"))) == 20

    second_report = await run_baseline(mode="fixture", output_root=tmp_path)
    assert second_report.run_id != report.run_id
    assert len(list((tmp_path / "fixture").iterdir())) == 2
