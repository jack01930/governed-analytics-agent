from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from governed_analytics.evals import week2_runner
from governed_analytics.evals.models import GeneratedSql, QueryResult
from governed_analytics.evals.pricing import estimate_cost_cny, load_model_pricing
from governed_analytics.evals.suites import EvaluationCase, load_active_suites, resolved_oracle_path
from governed_analytics.evals.week2_models import ComparisonReference
from governed_analytics.evals.week2_reporting import (
    pricing_snapshot_sha256,
    reference_report_sha256,
)
from governed_analytics.models.openai_compatible import (
    ModelAdapterError,
    OpenAICompatibleSqlGenerator,
)
from governed_analytics.models.protocols import EvaluationGenerationRequest, SqlGenerationRequest
from governed_analytics.safety.sql_policy import (
    SqlPolicyError,
    SqlRejectionCode,
    ValidatedSql,
    validate_sql,
)


def _expected_results(cases: tuple[EvaluationCase, ...]) -> dict[str, QueryResult]:
    return {
        case.case_id: QueryResult.model_validate_json(
            week2_runner._expected_path(case, cases).read_text(encoding="utf-8"), strict=True
        )
        for case in cases
        if case.expected_behavior == "execute"
    }


def _configure_common_preflights(
    monkeypatch: pytest.MonkeyPatch,
    cases: tuple[EvaluationCase, ...],
) -> dict[str, QueryResult]:
    expected = _expected_results(cases)
    monkeypatch.setattr(week2_runner, "load_active_suites", lambda: cases)
    monkeypatch.setattr(week2_runner, "_load_expected_results", lambda _cases: expected)
    monkeypatch.setattr(
        week2_runner, "_load_manifest", lambda _path: SimpleNamespace(dataset_id="a" * 64)
    )
    monkeypatch.setattr(
        week2_runner,
        "_load_reference",
        lambda _path: SimpleNamespace(
            run_id="run-v2",
            prompt_version="baseline-v2",
            dataset_id="a" * 64,
        ),
    )
    monkeypatch.setattr(week2_runner, "_suite_manifest_sha256", lambda _cases: "b" * 64)
    monkeypatch.setattr(week2_runner, "_implementation_sha256", lambda: "c" * 64)
    monkeypatch.setattr(week2_runner, "reference_report_sha256", lambda _reference: "d" * 64)
    monkeypatch.setattr(week2_runner, "build_schema_context", lambda: "schema-v1")
    monkeypatch.setattr(week2_runner, "build_metric_context", lambda: "metrics-v1")
    monkeypatch.setattr(
        week2_runner,
        "_comparison_reference",
        lambda *_args, **_kwargs: ComparisonReference(
            reference_run_id="run-v2",
            reference_protocol="baseline-v2",
            reference_dataset_id="a" * 64,
            reference_report_sha256="d" * 64,
            comparison_status="not_comparable",
        ),
    )
    return expected


def _expected_by_validated_sql(
    cases: tuple[EvaluationCase, ...], expected: dict[str, QueryResult]
) -> dict[str, QueryResult]:
    results: dict[str, QueryResult] = {}
    for case in cases:
        if case.expected_behavior == "execute":
            sql = resolved_oracle_path(case, cases).read_text(encoding="utf-8").split("\n", 1)[1]
            results[validate_sql(sql).sql] = expected[case.case_id]
    return results


def test_implementation_sha256_is_deterministic_and_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    included = {
        "src/package/app.py": "source-v1",
        "migrations/versions/0001.py": "migration-v1",
        "data/tool_catalog.yaml": "catalog-v1",
        "config/runtime.yaml": "config-v1",
        "infra/docker/postgres/init/001_bootstrap.sql": "bootstrap-v1",
        "Makefile": "make-v1",
        "alembic.ini": "alembic-v1",
        "docker-compose.yml": "compose-v1",
        "pyproject.toml": "project-v1",
        "uv.lock": "lock-v1",
    }
    excluded = {
        "artifacts/evals/report.json": "artifact-v1",
        "tests/unit/test_app.py": "test-v1",
        "docs/evals.md": "docs-v1",
        "data/secrets/provider.yaml": "secret-v1",
        "src/package/__pycache__/app.pyc": "cache-v1",
        "config/.env.local": "env-v1",
    }
    for relative, contents in included.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    for relative, contents in excluded.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    monkeypatch.setattr(week2_runner, "_REPOSITORY_ROOT", tmp_path)
    baseline = week2_runner._implementation_sha256()

    assert week2_runner._implementation_sha256() == baseline
    (tmp_path / "src/package/app.py").touch()
    assert week2_runner._implementation_sha256() == baseline
    for relative, contents in included.items():
        path = tmp_path / relative
        path.write_text(f"{contents}-changed", encoding="utf-8")
        assert week2_runner._implementation_sha256() != baseline
        path.write_text(contents, encoding="utf-8")
    for relative, contents in excluded.items():
        path = tmp_path / relative
        path.write_text(f"{contents}-changed", encoding="utf-8")
        assert week2_runner._implementation_sha256() == baseline


@pytest.mark.parametrize("missing_directory", ("src", "migrations", "data", "infra"))
def test_implementation_sha256_requires_every_execution_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_directory: str
) -> None:
    for directory in ("src", "migrations", "data", "infra"):
        if directory != missing_directory:
            (tmp_path / directory).mkdir()
    for filename in ("Makefile", "pyproject.toml", "uv.lock"):
        (tmp_path / filename).write_text(filename, encoding="utf-8")

    monkeypatch.setattr(week2_runner, "_REPOSITORY_ROOT", tmp_path)

    with pytest.raises(week2_runner.Week2RunError, match="implementation manifest"):
        week2_runner._implementation_sha256()


def test_pricing_snapshot_sha256_covers_every_canonical_pricing_field() -> None:
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    baseline = pricing_snapshot_sha256(pricing)
    updates: tuple[dict[str, object], ...] = (
        {"provider": "other-provider"},
        {"region": "other-region"},
        {"requested_model": "other-request"},
        {"resolved_model": "other-snapshot"},
        {"effective_date": date(2026, 9, 2)},
        {"currency": "USD"},
        {"unit_tokens": pricing.unit_tokens + 1},
        {"input_token_upper_bound": pricing.input_token_upper_bound + 1},
        {"input_price": pricing.input_price + Decimal("0.000001")},
        {"output_price": pricing.output_price + Decimal("0.000001")},
        {"pricing_basis": "other-basis"},
        {"source": "https://example.com/pricing"},
        {"fx_source": None},
    )

    assert len(baseline) == 64
    assert pricing_snapshot_sha256(pricing) == baseline
    assert all(
        pricing_snapshot_sha256(pricing.model_copy(update=update)) != baseline for update in updates
    )


def test_reference_report_sha256_binds_canonical_week1_evidence() -> None:
    reference = week2_runner._load_reference("docs/reports/evidence/v2/report.json")
    baseline = reference_report_sha256(reference)
    changed_first_case = reference.cases[0].model_copy(
        update={"latency_ms": reference.cases[0].latency_ms + 1}
    )
    changed_reference = reference.model_copy(
        update={"cases": (changed_first_case, *reference.cases[1:])}
    )

    assert len(baseline) == 64
    assert reference_report_sha256(reference) == baseline
    assert reference_report_sha256(changed_reference) != baseline


@pytest.mark.asyncio
async def test_fixture_runs_exactly_fifty_execute_cases_and_twenty_direct_safety_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    expected = _configure_common_preflights(monkeypatch, cases)
    generated_case_ids: list[str] = []
    executor_calls: list[str] = []

    class SpyFixtureGenerator:
        def __init__(self, sql_by_case_id: dict[str, str]) -> None:
            self.sql_by_case_id = sql_by_case_id

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            case_id = request.case_id
            generated_case_ids.append(case_id)
            return GeneratedSql(
                sql=self.sql_by_case_id[case_id],
                assumptions=("fixture-marker-must-not-publish",),
                provider_model="fixture-oracle",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            )

    expected_by_sql = _expected_by_validated_sql(cases, expected)

    async def fake_executor(sql: str) -> week2_runner.Week2Execution:
        executor_calls.append(sql)
        return week2_runner.Week2Execution(
            query_id=validate_sql(sql).query_id, result=expected_by_sql[sql]
        )

    monkeypatch.setattr(week2_runner, "Week2FixtureSqlGenerator", SpyFixtureGenerator)
    now_calls = 0

    def fixed_now() -> datetime:
        nonlocal now_calls
        now_calls += 1
        return datetime(2026, 9, 4, tzinfo=UTC)

    report = await week2_runner.run_week2_evaluation(
        mode="fixture",
        output_root=tmp_path,
        executor=fake_executor,
        run_id="fixture-70",
        now=fixed_now,
    )

    assert len(generated_case_ids) == 50
    assert len(executor_calls) == 50
    assert now_calls == 1
    assert {case.case_id for case in report.cases if case.suite == "safety"} == {
        f"S2{number:02d}" for number in range(1, 21)
    }
    assert all(case.status == "rejected" for case in report.cases if case.suite == "safety")
    assert report.total_cost_cny == 0
    assert report.total_input_tokens == 0
    assert report.total_output_tokens == 0
    assert report.implementation_sha256 == "c" * 64
    assert report.pricing_requested_model is None
    assert report.pricing_resolved_model is None
    assert report.pricing_snapshot_sha256 is None
    assert report.comparison_reference.reference_report_sha256 == "d" * 64
    assert report.cost_estimate_complete
    assert report.unpriced_call_count == 0
    report_text = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.*"))
    assert "fixture-marker-must-not-publish" not in report_text
    assert "select " not in report_text.lower()


@pytest.mark.asyncio
async def test_live_prices_only_errors_with_complete_usage_and_preserves_telemetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    expected = _configure_common_preflights(monkeypatch, cases)
    oracle_sql = {
        case.case_id: resolved_oracle_path(case, cases)
        .read_text(encoding="utf-8")
        .split("\n", 1)[1]
        for case in cases
        if case.expected_behavior == "execute"
    }
    expected_by_sql = _expected_by_validated_sql(cases, expected)
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")

    class FakeLiveGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return "deepseek-v4-flash"

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            case_id = request.case_id
            if case_id == "C201":
                raise ModelAdapterError(
                    "invalid_json",
                    provider_model="deepseek-v4-flash",
                    input_tokens=11,
                    output_tokens=12,
                    latency_ms=13,
                    finish_reason="length",
                    output_truncated=True,
                )
            if case_id == "C203":
                raise ModelAdapterError(
                    "missing_usage",
                    provider_model="deepseek-v4-flash",
                    latency_ms=14,
                )
            if case_id == "C204":
                raise ModelAdapterError(
                    "invalid_json",
                    provider_model="deepseek-v4-flash",
                    latency_ms=15,
                )
            if case_id == "C205":
                raise ModelAdapterError(
                    "invalid_usage",
                    provider_model="deepseek-v4-flash",
                    input_tokens=11,
                    latency_ms=16,
                )
            return GeneratedSql(
                sql=oracle_sql[case_id],
                provider_model="deepseek-v4-flash",
                input_tokens=2,
                output_tokens=3,
                latency_ms=4,
                finish_reason="length" if case_id == "C202" else "stop",
                output_truncated=case_id == "C202",
            )

    async def fake_executor(sql: str) -> week2_runner.Week2Execution:
        return week2_runner.Week2Execution(
            query_id=validate_sql(sql).query_id, result=expected_by_sql[sql]
        )

    report = await week2_runner.run_week2_evaluation(
        mode="live",
        output_root=tmp_path,
        generator=FakeLiveGenerator.__new__(FakeLiveGenerator),
        pricing=pricing,
        executor=fake_executor,
        run_id="live-telemetry",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    failed = next(case for case in report.cases if case.case_id == "C201")
    completed = next(case for case in report.cases if case.case_id == "C202")
    missing_usage = next(case for case in report.cases if case.case_id == "C203")
    content_error_without_usage = next(case for case in report.cases if case.case_id == "C204")
    partial_usage = next(case for case in report.cases if case.case_id == "C205")
    assert failed.error_type == "generation_invalid_json"
    assert failed.finish_reason == "length" and failed.output_truncated
    assert failed.estimated_cost_cny == estimate_cost_cny(11, 12, pricing)
    assert completed.finish_reason == "length" and completed.output_truncated
    assert completed.estimated_cost_cny == estimate_cost_cny(2, 3, pricing)
    assert missing_usage.error_type == "pricing_failed"
    assert missing_usage.estimated_cost_cny == 0
    assert content_error_without_usage.error_type == "pricing_failed"
    assert content_error_without_usage.estimated_cost_cny == 0
    assert partial_usage.error_type == "pricing_failed"
    assert partial_usage.input_tokens == 11
    assert partial_usage.output_tokens == 0
    assert partial_usage.estimated_cost_cny == 0
    assert report.truncated_generation_count == 2
    assert report.pricing_requested_model == pricing.requested_model
    assert report.pricing_resolved_model == pricing.resolved_model
    assert report.pricing_snapshot_sha256 == pricing_snapshot_sha256(pricing)
    assert not report.cost_estimate_complete
    assert report.unpriced_call_count == 3


@pytest.mark.asyncio
async def test_live_rejects_mismatched_pricing_before_a_generator_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    _configure_common_preflights(monkeypatch, cases)
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    generator_calls = 0

    class MismatchedLiveGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return "different-model"

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            nonlocal generator_calls
            generator_calls += 1
            raise AssertionError(request.case_id)

    with pytest.raises(week2_runner.Week2RunError, match="pricing metadata unavailable"):
        await week2_runner.run_week2_evaluation(
            mode="live",
            output_root=tmp_path,
            generator=MismatchedLiveGenerator.__new__(MismatchedLiveGenerator),
            pricing=pricing,
        )

    assert generator_calls == 0
    assert not tuple(tmp_path.rglob("report.json"))


@pytest.mark.parametrize(
    ("pricing_update", "generator_model"),
    (
        ({"requested_model": "sk-secret-model"}, "sk-secret-model"),
        ({"resolved_model": "sk-secret-snapshot"}, "deepseek-v4-flash"),
        ({"resolved_model": "fixture-oracle"}, "deepseek-v4-flash"),
        ({"pricing_basis": "unapproved-basis"}, "deepseek-v4-flash"),
        ({"source": "not-a-valid-url"}, "deepseek-v4-flash"),
    ),
    ids=("unsafe_requested", "unsafe_resolved", "fixture_resolved", "basis", "invalid_record"),
)
@pytest.mark.asyncio
async def test_live_rejects_invalid_pricing_metadata_before_a_generator_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pricing_update: dict[str, object],
    generator_model: str,
) -> None:
    cases = load_active_suites()
    _configure_common_preflights(monkeypatch, cases)
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml").model_copy(
        update=pricing_update
    )
    generator_calls = 0

    class InvalidMetadataGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return generator_model

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            nonlocal generator_calls
            generator_calls += 1
            raise AssertionError(request.case_id)

    with pytest.raises(week2_runner.Week2RunError, match="pricing metadata unavailable"):
        await week2_runner.run_week2_evaluation(
            mode="live",
            output_root=tmp_path,
            generator=InvalidMetadataGenerator.__new__(InvalidMetadataGenerator),
            pricing=pricing,
        )

    assert generator_calls == 0
    assert not tuple(tmp_path.rglob("report.json"))


@pytest.mark.asyncio
async def test_live_precomputes_pricing_snapshot_before_a_generator_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    _configure_common_preflights(monkeypatch, cases)
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    generator_calls = 0

    class NoCallGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return pricing.requested_model

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            nonlocal generator_calls
            generator_calls += 1
            raise AssertionError(request.case_id)

    def fail_snapshot(_pricing: object) -> str:
        raise ValueError("injected snapshot failure")

    monkeypatch.setattr(week2_runner, "pricing_snapshot_sha256", fail_snapshot)

    with pytest.raises(week2_runner.Week2RunError, match="pricing metadata unavailable"):
        await week2_runner.run_week2_evaluation(
            mode="live",
            output_root=tmp_path,
            generator=NoCallGenerator.__new__(NoCallGenerator),
            pricing=pricing,
        )

    assert generator_calls == 0
    assert not tuple(tmp_path.rglob("report.json"))


@pytest.mark.parametrize(
    "metadata_failure",
    (
        "invalid_run_id",
        "empty_run_id",
        "long_run_id",
        "secret_run_id",
        "naive_time",
        "unsafe_reference",
        "reference_hash",
    ),
)
@pytest.mark.asyncio
async def test_live_rejects_known_report_metadata_before_a_generator_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata_failure: str,
) -> None:
    cases = load_active_suites()
    _configure_common_preflights(monkeypatch, cases)
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    generator_calls = 0

    if metadata_failure == "unsafe_reference":
        monkeypatch.setattr(
            week2_runner,
            "_load_reference",
            lambda _path: SimpleNamespace(
                run_id="run-v2",
                prompt_version="sk-secret-reference",
                dataset_id="a" * 64,
            ),
        )
    elif metadata_failure == "reference_hash":

        def fail_reference_hash(_reference: object) -> str:
            raise ValueError("injected reference hash failure")

        monkeypatch.setattr(week2_runner, "reference_report_sha256", fail_reference_hash)
    active_run_id = {
        "invalid_run_id": "invalid|run",
        "empty_run_id": "",
        "long_run_id": "r" * 129,
        "secret_run_id": "sk-secret-run",
    }.get(metadata_failure, "safe-run")
    generated_at = (
        datetime(2026, 9, 4)
        if metadata_failure == "naive_time"
        else datetime(2026, 9, 4, tzinfo=UTC)
    )

    class NoCallGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return pricing.requested_model

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            nonlocal generator_calls
            generator_calls += 1
            raise AssertionError(request.case_id)

    with pytest.raises(week2_runner.Week2RunError, match="report metadata unavailable"):
        await week2_runner.run_week2_evaluation(
            mode="live",
            output_root=tmp_path,
            generator=NoCallGenerator.__new__(NoCallGenerator),
            pricing=pricing,
            run_id=active_run_id,
            now=lambda: generated_at,
        )

    assert generator_calls == 0
    assert not tuple(tmp_path.rglob("report.json"))


@pytest.mark.parametrize(
    "publication_failure",
    (
        "collision",
        "unusable_output_root",
        "atomic_unavailable",
        "atomic_overwrites_target",
    ),
)
@pytest.mark.asyncio
async def test_live_reserves_publication_before_a_generator_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    publication_failure: str,
) -> None:
    cases = load_active_suites()
    _configure_common_preflights(monkeypatch, cases)
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    generated_at = datetime(2026, 9, 4, tzinfo=UTC)
    active_run_id = "publication-preflight"
    output_root = tmp_path
    generator_calls = 0
    secret_marker = "unique-publication-probe-secret"

    if publication_failure == "collision":
        target = tmp_path / "live" / f"20260904T000000Z-{active_run_id}"
        target.mkdir(parents=True)
    elif publication_failure == "unusable_output_root":
        output_root = tmp_path / "not-a-directory"
        output_root.write_text("blocked", encoding="utf-8")
    elif publication_failure == "atomic_unavailable":

        def fail_atomic_publication(_source: Path, _target: Path) -> None:
            raise OSError(secret_marker)

        monkeypatch.setattr(
            "governed_analytics.evals.week2_reporting._publish_staged_directory",
            fail_atomic_publication,
        )
    else:

        def unsafe_replace(source: Path, target: Path) -> None:
            source.replace(target)

        monkeypatch.setattr(
            "governed_analytics.evals.week2_reporting._publish_staged_directory",
            unsafe_replace,
        )

    class NoCallGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return pricing.requested_model

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            nonlocal generator_calls
            generator_calls += 1
            raise AssertionError(request.case_id)

    with pytest.raises(
        week2_runner.Week2RunError, match="report publication unavailable"
    ) as caught:
        await week2_runner.run_week2_evaluation(
            mode="live",
            output_root=output_root,
            generator=NoCallGenerator.__new__(NoCallGenerator),
            pricing=pricing,
            run_id=active_run_id,
            now=lambda: generated_at,
        )

    assert secret_marker not in str(caught.value)
    assert generator_calls == 0
    assert not tuple(tmp_path.rglob("report.json"))
    assert not tuple(tmp_path.rglob("*staging-reservation*"))
    assert not tuple(tmp_path.rglob(".week2-atomic-probe-*"))
    assert not tuple(tmp_path.rglob(".week2-no-replace-*"))


@pytest.mark.asyncio
async def test_publication_reservation_is_cleaned_for_an_interrupted_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    _configure_common_preflights(monkeypatch, cases)
    generator_calls = 0

    class InjectedInterruption(BaseException):
        pass

    class InterruptingFixtureGenerator:
        def __init__(self, _sql_by_case_id: dict[str, str]) -> None:
            pass

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            nonlocal generator_calls
            generator_calls += 1
            raise InjectedInterruption(request.case_id)

    monkeypatch.setattr(
        week2_runner,
        "Week2FixtureSqlGenerator",
        InterruptingFixtureGenerator,
    )

    with pytest.raises(InjectedInterruption):
        await week2_runner.run_week2_evaluation(
            mode="fixture",
            output_root=tmp_path,
            run_id="interrupted-reservation",
            now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert generator_calls == 1
    assert not tuple(tmp_path.rglob("report.json"))
    assert not tuple(tmp_path.rglob("*staging-reservation*"))
    assert not tuple(tmp_path.rglob(".week2-atomic-probe-*"))


@pytest.mark.parametrize(
    ("adapter_error", "provider_model", "record_provider_model"),
    (
        (False, "unpriced-provider-snapshot", True),
        (True, "unpriced-provider-snapshot", True),
        (True, None, False),
        (False, "sk-unsafe-response-model", False),
        (True, "sk-unsafe-error-model", False),
        (False, "fixture-oracle", True),
    ),
    ids=(
        "successful_response",
        "adapter_error",
        "adapter_error_without_model",
        "unsafe_successful_response",
        "unsafe_adapter_error",
        "safe_fixture_named_response",
    ),
)
@pytest.mark.asyncio
async def test_live_unpriced_provider_model_fails_pricing_without_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    adapter_error: bool,
    provider_model: str | None,
    record_provider_model: bool,
) -> None:
    cases = load_active_suites()
    expected = _configure_common_preflights(monkeypatch, cases)
    expected_by_sql = _expected_by_validated_sql(cases, expected)
    oracle_sql = {
        case.case_id: resolved_oracle_path(case, cases)
        .read_text(encoding="utf-8")
        .split("\n", 1)[1]
        for case in cases
        if case.expected_behavior == "execute"
    }
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    executor_calls: list[str] = []

    class UnexpectedProviderGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return pricing.requested_model

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            if request.case_id == "C201":
                if adapter_error:
                    raise ModelAdapterError(
                        "invalid_json",
                        provider_model=provider_model,
                        input_tokens=11,
                        output_tokens=12,
                        latency_ms=13,
                    )
                assert provider_model is not None
                return GeneratedSql(
                    sql=oracle_sql[request.case_id],
                    provider_model=provider_model,
                    input_tokens=11,
                    output_tokens=12,
                    latency_ms=13,
                )
            return GeneratedSql(
                sql=oracle_sql[request.case_id],
                provider_model=pricing.resolved_model,
                input_tokens=2,
                output_tokens=3,
                latency_ms=4,
            )

    async def fake_executor(sql: str) -> week2_runner.Week2Execution:
        executor_calls.append(sql)
        return week2_runner.Week2Execution(
            query_id=validate_sql(sql).query_id, result=expected_by_sql[sql]
        )

    report = await week2_runner.run_week2_evaluation(
        mode="live",
        output_root=tmp_path,
        generator=UnexpectedProviderGenerator.__new__(UnexpectedProviderGenerator),
        pricing=pricing,
        executor=fake_executor,
        run_id=f"unpriced-{'error' if adapter_error else 'response'}",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    unpriced = next(case for case in report.cases if case.case_id == "C201")
    assert unpriced.status == "invalid_sql"
    assert unpriced.error_type == "pricing_failed"
    assert unpriced.estimated_cost_cny == 0
    assert unpriced.query_id is None
    assert len(executor_calls) == 49
    expected_resolved_models = {pricing.resolved_model}
    if record_provider_model:
        assert provider_model is not None
        expected_resolved_models.add(provider_model)
    assert set(report.resolved_models) == expected_resolved_models
    assert report.pricing_requested_model == pricing.requested_model
    assert report.pricing_resolved_model == pricing.resolved_model
    assert report.pricing_snapshot_sha256 == pricing_snapshot_sha256(pricing)
    assert not report.cost_estimate_complete
    assert report.unpriced_call_count == 1
    assert report.comparison_reference.comparison_status == "not_comparable"


@pytest.mark.asyncio
async def test_live_unknown_generation_failure_is_unpriced_and_not_executed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    expected = _configure_common_preflights(monkeypatch, cases)
    expected_by_sql = _expected_by_validated_sql(cases, expected)
    oracle_sql = {
        case.case_id: resolved_oracle_path(case, cases)
        .read_text(encoding="utf-8")
        .split("\n", 1)[1]
        for case in cases
        if case.expected_behavior == "execute"
    }
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    executor_calls: list[str] = []

    class UnknownFailureGenerator(OpenAICompatibleSqlGenerator):
        @property
        def model(self) -> str:
            return pricing.requested_model

        async def generate(
            self, request: SqlGenerationRequest | EvaluationGenerationRequest
        ) -> GeneratedSql:
            if request.case_id == "C201":
                raise RuntimeError("injected unknown provider failure")
            return GeneratedSql(
                sql=oracle_sql[request.case_id],
                provider_model=pricing.resolved_model,
                input_tokens=2,
                output_tokens=3,
                latency_ms=4,
            )

    async def fake_executor(sql: str) -> week2_runner.Week2Execution:
        executor_calls.append(sql)
        return week2_runner.Week2Execution(
            query_id=validate_sql(sql).query_id, result=expected_by_sql[sql]
        )

    report = await week2_runner.run_week2_evaluation(
        mode="live",
        output_root=tmp_path,
        generator=UnknownFailureGenerator.__new__(UnknownFailureGenerator),
        pricing=pricing,
        executor=fake_executor,
        run_id="unknown-generation-failure",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    unpriced = next(case for case in report.cases if case.case_id == "C201")
    assert unpriced.error_type == "pricing_failed"
    assert unpriced.input_tokens == 0
    assert unpriced.output_tokens == 0
    assert unpriced.estimated_cost_cny == 0
    assert len(executor_calls) == 49
    assert not report.cost_estimate_complete
    assert report.unpriced_call_count == 1
    assert report.comparison_reference.comparison_status == "not_comparable"


@pytest.mark.asyncio
async def test_safety_rejection_code_mismatch_is_reported_strictly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = load_active_suites()
    expected = _configure_common_preflights(monkeypatch, cases)
    expected_by_sql = _expected_by_validated_sql(cases, expected)
    original_validate = validate_sql

    class SpyFixtureGenerator:
        def __init__(self, sql_by_case_id: dict[str, str]) -> None:
            self.sql_by_case_id = sql_by_case_id

        async def generate(self, request: EvaluationGenerationRequest) -> GeneratedSql:
            return GeneratedSql(
                sql=self.sql_by_case_id[request.case_id],
                provider_model="fixture-oracle",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            )

    def mismatching_validate(sql: str) -> ValidatedSql:
        if sql == "create table exfiltration (id int)":
            raise SqlPolicyError(SqlRejectionCode.INVALID_SQL)
        return original_validate(sql)

    async def fake_executor(sql: str) -> week2_runner.Week2Execution:
        return week2_runner.Week2Execution(
            query_id=original_validate(sql).query_id, result=expected_by_sql[sql]
        )

    monkeypatch.setattr(week2_runner, "Week2FixtureSqlGenerator", SpyFixtureGenerator)
    monkeypatch.setattr(week2_runner, "validate_sql", mismatching_validate)
    report = await week2_runner.run_week2_evaluation(
        mode="fixture",
        output_root=tmp_path,
        executor=fake_executor,
        run_id="safety-mismatch",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    mismatch = next(case for case in report.cases if case.case_id == "S201")
    assert mismatch.status == "rejection_mismatch"
    assert mismatch.expected_rejection == SqlRejectionCode.NOT_READONLY_QUERY.value
    assert mismatch.observed_rejection == SqlRejectionCode.INVALID_SQL.value
    assert mismatch.error_type == "safety_rejection_mismatch"


def test_boundary_cases_resolve_to_checked_expected_json_paths() -> None:
    cases = load_active_suites()
    boundary = tuple(case for case in cases if case.suite == "boundary")

    assert len(boundary) == 10
    assert [week2_runner._expected_path(case, cases).name for case in boundary] == [
        f"B2{number:02d}.json" for number in range(1, 11)
    ]
    assert all(week2_runner._expected_path(case, cases).is_file() for case in boundary)


def test_historical_comparison_requires_same_live_resolved_model_and_pricing() -> None:
    reference = week2_runner._load_reference("docs/reports/evidence/v2/report.json")
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")
    reference_digest = reference_report_sha256(reference)

    comparable = week2_runner._comparison_reference(
        reference,
        mode="live",
        dataset_id=reference.dataset_id,
        prompt_version=reference.prompt_version,
        requested_model=reference.requested_model,
        resolved_models=reference.resolved_models,
        pricing=pricing,
        cost_estimate_complete=True,
        reference_report_digest=reference_digest,
    )
    different_model = week2_runner._comparison_reference(
        reference,
        mode="live",
        dataset_id=reference.dataset_id,
        prompt_version=reference.prompt_version,
        requested_model=reference.requested_model,
        resolved_models=("different-provider-snapshot",),
        pricing=pricing,
        cost_estimate_complete=True,
        reference_report_digest=reference_digest,
    )
    fixture_report = reference.model_copy(update={"mode": "fixture"})
    fixture_reference = week2_runner._comparison_reference(
        fixture_report,
        mode="live",
        dataset_id=reference.dataset_id,
        prompt_version=reference.prompt_version,
        requested_model=reference.requested_model,
        resolved_models=reference.resolved_models,
        pricing=pricing,
        cost_estimate_complete=True,
        reference_report_digest=reference_report_sha256(fixture_report),
    )
    different_pricing = week2_runner._comparison_reference(
        reference,
        mode="live",
        dataset_id=reference.dataset_id,
        prompt_version=reference.prompt_version,
        requested_model=reference.requested_model,
        resolved_models=reference.resolved_models,
        pricing=pricing.model_copy(update={"effective_date": date(2026, 9, 2)}),
        cost_estimate_complete=True,
        reference_report_digest=reference_digest,
    )
    incomplete_cost = week2_runner._comparison_reference(
        reference,
        mode="live",
        dataset_id=reference.dataset_id,
        prompt_version=reference.prompt_version,
        requested_model=reference.requested_model,
        resolved_models=reference.resolved_models,
        pricing=pricing,
        cost_estimate_complete=False,
        reference_report_digest=reference_digest,
    )
    total_input_tokens = sum(case.input_tokens for case in reference.cases)
    total_output_tokens = sum(case.output_tokens for case in reference.cases)
    offset = Decimal("0.000000000001")
    offsetting_prices = pricing.model_copy(
        update={
            "input_price": pricing.input_price + Decimal(total_output_tokens) * offset,
            "output_price": pricing.output_price - Decimal(total_input_tokens) * offset,
        }
    )
    changed_prices = week2_runner._comparison_reference(
        reference,
        mode="live",
        dataset_id=reference.dataset_id,
        prompt_version=reference.prompt_version,
        requested_model=reference.requested_model,
        resolved_models=reference.resolved_models,
        pricing=offsetting_prices,
        cost_estimate_complete=True,
        reference_report_digest=reference_digest,
    )
    repriced_total = sum(
        (
            estimate_cost_cny(case.input_tokens, case.output_tokens, offsetting_prices)
            for case in reference.cases
        ),
        Decimal("0"),
    )

    assert comparable.comparison_status == "comparable"
    assert different_model.comparison_status == "not_comparable"
    assert fixture_reference.comparison_status == "not_comparable"
    assert different_pricing.comparison_status == "not_comparable"
    assert incomplete_cost.comparison_status == "not_comparable"
    assert repriced_total == reference.total_cost_cny
    assert changed_prices.comparison_status == "not_comparable"

    with pytest.raises(ValueError, match="digest does not match"):
        week2_runner._comparison_reference(
            reference,
            mode="live",
            dataset_id=reference.dataset_id,
            prompt_version=reference.prompt_version,
            requested_model=reference.requested_model,
            resolved_models=reference.resolved_models,
            pricing=pricing,
            cost_estimate_complete=True,
            reference_report_digest="f" * 64,
        )


def test_comparison_reference_rejects_secret_shaped_historical_protocol() -> None:
    reference = week2_runner._load_reference("docs/reports/evidence/v2/report.json")
    unsafe = reference.model_copy(update={"prompt_version": "sk-0123456789abcdef0123456789abcdef"})

    with pytest.raises(ValidationError, match="unsafe metadata"):
        week2_runner._comparison_reference(
            unsafe,
            mode="fixture",
            dataset_id=reference.dataset_id,
            prompt_version=reference.prompt_version,
            requested_model="fixture-oracle",
            resolved_models=("fixture-oracle",),
            pricing=None,
            cost_estimate_complete=True,
            reference_report_digest=reference_report_sha256(unsafe),
        )


@pytest.mark.asyncio
async def test_fixture_loads_repository_expected_json_without_test_preflight_patch(
    tmp_path: Path,
) -> None:
    cases = load_active_suites()
    expected = _expected_results(cases)
    expected_by_sql = _expected_by_validated_sql(cases, expected)

    async def fake_executor(sql: str) -> week2_runner.Week2Execution:
        return week2_runner.Week2Execution(
            query_id=validate_sql(sql).query_id, result=expected_by_sql[sql]
        )

    report = await week2_runner.run_week2_evaluation(
        mode="fixture", output_root=tmp_path, executor=fake_executor
    )

    assert len(report.cases) == 70
    assert {case.status for case in report.cases} == {"passed", "rejected"}
    reference = week2_runner._load_reference("docs/reports/evidence/v2/report.json")
    assert report.comparison_reference.reference_report_sha256 == reference_report_sha256(reference)
