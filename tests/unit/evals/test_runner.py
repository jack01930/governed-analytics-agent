from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from shutil import copytree

import pytest

from governed_analytics.evals.models import GeneratedSql, QueryResult
from governed_analytics.evals.runner import BaselineRunError, _load_expected_results, run_baseline
from governed_analytics.evals.sql_guard import SqlRejected
from governed_analytics.models.openai_compatible import ModelAdapterError
from governed_analytics.models.protocols import SqlGenerationRequest


class _Generator:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def generate(self, request: SqlGenerationRequest) -> GeneratedSql:
        case_id = request.case_id
        self.calls.append(case_id)
        outcome = self.outcomes.get(case_id)
        if isinstance(outcome, Exception):
            raise outcome
        return (
            outcome
            if isinstance(outcome, GeneratedSql)
            else GeneratedSql(
                sql="select 1 as gmv",
                provider_model="fake-model",
                input_tokens=2,
                output_tokens=3,
                latency_ms=4,
            )
        )


@pytest.mark.asyncio
async def test_runner_calls_each_boundary_once_and_continues_after_case_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generator = _Generator(
        {
            "G001": RuntimeError("secret"),
            "G002": GeneratedSql(
                sql="select rejected",
                provider_model="fake-model",
                input_tokens=2,
                output_tokens=3,
                latency_ms=4,
            ),
        }
    )
    executor_calls: list[str] = []

    async def fake_executor(sql: str) -> QueryResult:
        executor_calls.append(sql)
        if sql == "select rejected":
            raise SqlRejected("baseline SQL rejected")
        return QueryResult(columns=("gmv",), rows=((Decimal("1"),),))

    monkeypatch.setattr(
        "governed_analytics.evals.runner.FixtureSqlGenerator.from_path",
        lambda *_args: generator,
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.write_baseline_report",
        lambda *_args, **_kwargs: tmp_path,
    )
    report = await run_baseline(
        mode="fixture",
        output_root=tmp_path,
        executor=fake_executor,
        run_id="unit",
        now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    assert len(generator.calls) == 20
    assert len(executor_calls) == 19
    assert report.cases[0].status == "invalid_sql"
    assert report.cases[0].generated_sql is None
    assert report.cases[0].error_type == "generation_failed"
    assert report.cases[1].status == "invalid_sql"
    assert report.cases[1].error_type == "sql_rejected"


@pytest.mark.asyncio
async def test_fixture_rejects_injected_generator_without_calling_it(tmp_path: Path) -> None:
    generator = _Generator({})
    with pytest.raises(BaselineRunError, match=r"^fixture generator injection is unavailable$"):
        await run_baseline(mode="fixture", output_root=tmp_path, generator=generator)
    assert generator.calls == []


def test_expected_truth_requires_exact_twenty_fixed_case_files(tmp_path: Path) -> None:
    expected_dir = Path("evals/datasets/golden/expected")
    copied = tmp_path / "expected"
    copytree(expected_dir, copied)
    (copied / "G020.json").unlink()
    with pytest.raises(BaselineRunError, match=r"^baseline truth unavailable$"):
        _load_expected_results(copied)

    copytree(expected_dir, copied, dirs_exist_ok=True)
    (copied / "G021.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BaselineRunError, match=r"^baseline truth unavailable$"):
        _load_expected_results(copied)

    (copied / "G021.json").unlink()
    (copied / "G001.json").write_text(
        '{"columns":["first"],"columns":["gmv"],"rows":[["1"]]}', encoding="utf-8"
    )
    with pytest.raises(BaselineRunError, match=r"^baseline truth unavailable$") as error:
        _load_expected_results(copied)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_runner_preserves_model_adapter_category_and_sanitizes_publish_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generator = _Generator({"G001": ModelAdapterError("invalid_content")})

    async def fake_executor(_sql: str) -> QueryResult:
        return QueryResult(columns=("gmv",), rows=((Decimal("1"),),))

    monkeypatch.setattr(
        "governed_analytics.evals.runner.FixtureSqlGenerator.from_path",
        lambda *_args: generator,
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.write_baseline_report",
        lambda *_args, **_kwargs: tmp_path,
    )
    report = await run_baseline(
        mode="fixture",
        output_root=tmp_path,
        executor=fake_executor,
    )
    assert report.cases[0].error_type == "generation_invalid_content"

    monkeypatch.setattr(
        "governed_analytics.evals.runner.write_baseline_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret endpoint")),
    )
    with pytest.raises(BaselineRunError, match=r"^baseline report publication failed$") as error:
        await run_baseline(
            mode="fixture",
            output_root=tmp_path,
            executor=fake_executor,
        )
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_runner_continues_after_scoring_failure_without_raw_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generator = _Generator({})
    executor_calls: list[str] = []

    async def fake_executor(sql: str) -> QueryResult:
        executor_calls.append(sql)
        return QueryResult(columns=("gmv",), rows=((Decimal("1"),),))

    monkeypatch.setattr(
        "governed_analytics.evals.runner.FixtureSqlGenerator.from_path",
        lambda *_args: generator,
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.score_result",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("score-marker")),
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.write_baseline_report",
        lambda *_args, **_kwargs: tmp_path,
    )
    report = await run_baseline(mode="fixture", output_root=tmp_path, executor=fake_executor)

    assert len(generator.calls) == 20
    assert len(executor_calls) == 20
    assert [case.case_id for case in report.cases] == [f"G{number:03d}" for number in range(1, 21)]
    assert all(case.status == "execution_error" for case in report.cases)
    assert all(case.error_type == "scoring_failed" for case in report.cases)
    assert all("score-marker" not in str(case) for case in report.cases)


@pytest.mark.asyncio
async def test_report_prompt_identity_binds_exact_context_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generator = _Generator({})
    contexts = {"schema": "schema-v1", "metrics": "metrics-v1"}

    async def fake_executor(_sql: str) -> QueryResult:
        return QueryResult(columns=("gmv",), rows=((Decimal("1"),),))

    monkeypatch.setattr(
        "governed_analytics.evals.runner.FixtureSqlGenerator.from_path",
        lambda *_args: generator,
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.build_schema_context",
        lambda: contexts["schema"],
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.build_metric_context",
        lambda: contexts["metrics"],
    )
    monkeypatch.setattr(
        "governed_analytics.evals.runner.write_baseline_report",
        lambda *_args, **_kwargs: tmp_path,
    )
    first = await run_baseline(mode="fixture", output_root=tmp_path, executor=fake_executor)
    contexts["metrics"] = "metrics-v2"
    second = await run_baseline(mode="fixture", output_root=tmp_path, executor=fake_executor)

    assert first.prompt_version != second.prompt_version
    assert "sha256:" in first.prompt_version
