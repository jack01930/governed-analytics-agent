"""One-pass Week 2 evaluation across execute and direct safety suites."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from governed_analytics.data_generation.models import DatasetManifest, DatasetScale
from governed_analytics.evals.context import (
    EVALUATION_CONTEXT_VERSION,
    build_metric_context,
    build_schema_context,
    context_sha256,
)
from governed_analytics.evals.models import BaselineRunReport, GeneratedSql, QueryResult
from governed_analytics.evals.pricing import ModelPricing, estimate_cost_cny
from governed_analytics.evals.suites import (
    EvaluationCase,
    load_active_suites,
    resolved_oracle_path,
)
from governed_analytics.evals.week2_models import (
    WEEK2_FIXTURE_MODEL,
    WEEK2_PRICING_BASIS,
    ComparisonReference,
    Week2CaseResult,
    Week2CaseStatus,
    Week2RunReport,
    _safe_model_identifier,
    derive_week2_aggregates,
)
from governed_analytics.evals.week2_reporting import (
    Week2ReportReservation,
    build_week2_run_report,
    cancel_week2_report_reservation,
    pricing_snapshot_sha256,
    reference_report_sha256,
    reserve_week2_report,
    write_week2_report,
)
from governed_analytics.evals.week2_scorers import score_result_and_contract
from governed_analytics.models.fixtures import Week2FixtureSqlGenerator
from governed_analytics.models.openai_compatible import (
    ModelAdapterError,
    OpenAICompatibleSqlGenerator,
)
from governed_analytics.models.prompts import BASELINE_PROMPT_VERSION
from governed_analytics.models.protocols import (
    EvaluationGenerationRequest,
    EvaluationSqlGenerator,
)
from governed_analytics.safety.sql_policy import SqlPolicyError, validate_sql
from governed_analytics.tools import ErrorCode, ExecuteSqlRequest, ExecuteSqlTool

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_IMPLEMENTATION_DIRECTORIES = ("src", "migrations", "data", "config", "infra")
_REQUIRED_IMPLEMENTATION_DIRECTORIES = frozenset({"src", "migrations", "data", "infra"})
_IMPLEMENTATION_ROOT_FILES = (
    "Makefile",
    "alembic.ini",
    "docker-compose.yml",
    "pyproject.toml",
    "uv.lock",
)
_REQUIRED_IMPLEMENTATION_ROOT_FILES = frozenset({"Makefile", "pyproject.toml", "uv.lock"})
_IMPLEMENTATION_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "artifacts",
        "docs",
        "secrets",
        "tests",
    }
)
_IMPLEMENTATION_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
_REGISTRY_PATHS = (
    _REPOSITORY_ROOT / "evals/datasets/golden/core-v2/cases.yaml",
    _REPOSITORY_ROOT / "evals/datasets/golden/paraphrase/cases.yaml",
    _REPOSITORY_ROOT / "evals/datasets/golden/boundary/cases.yaml",
    _REPOSITORY_ROOT / "evals/datasets/safety/adversarial/cases.yaml",
)
_LEGACY_EXPECTED_DIR = _REPOSITORY_ROOT / "evals/datasets/golden/expected"
_BOUNDARY_EXPECTED_DIR = _REPOSITORY_ROOT / "evals/datasets/golden/boundary/expected"
_DEFAULT_MANIFEST_PATH = _REPOSITORY_ROOT / "artifacts/datasets/tiny/dataset_manifest.json"
_DEFAULT_REFERENCE_PATH = _REPOSITORY_ROOT / "docs/reports/evidence/v2/report.json"
_DEFAULT_REPORT_ROOT = _REPOSITORY_ROOT / "artifacts/evals/week2"
_MODEL_ERROR_CATEGORIES = frozenset(
    {
        "invalid_request",
        "provider_call_failed",
        "missing_content",
        "invalid_content",
        "invalid_content_type",
        "invalid_json",
        "invalid_envelope",
        "invalid_sql_content",
        "invalid_assumptions",
        "missing_usage",
        "invalid_usage",
        "missing_model",
    }
)


class Week2RunError(ValueError):
    """Sanitized global failure; no partial report is published."""


class Week2SqlRejected(ValueError):
    """Sanitized generated-SQL rejection raised by an injected executor."""


class Week2ExecutionError(ValueError):
    """Sanitized execution failure with a stable public category."""

    def __init__(self, category: str = "execution_failed") -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class Week2Execution:
    query_id: str
    result: QueryResult


type Week2Executor = Callable[[str], Awaitable[Week2Execution]]


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_manifest(path: str | Path) -> DatasetManifest:
    try:
        manifest = DatasetManifest.model_validate_json(
            _resolve(path).read_text(encoding="utf-8"), strict=True
        )
    except Exception:
        raise Week2RunError("tiny dataset manifest unavailable") from None
    if manifest.scale is not DatasetScale.TINY:
        raise Week2RunError("tiny dataset manifest unavailable")
    return manifest


def _load_reference(path: str | Path) -> BaselineRunReport:
    try:
        return BaselineRunReport.model_validate_json(
            _resolve(path).read_text(encoding="utf-8"), strict=True
        )
    except Exception:
        raise Week2RunError("Week 1 comparison reference unavailable") from None


def _expected_path(case: EvaluationCase, cases: tuple[EvaluationCase, ...]) -> Path:
    if case.expected_result_path is not None:
        return case.expected_result_path
    stem = resolved_oracle_path(case, cases).stem
    if stem.startswith("G"):
        return _LEGACY_EXPECTED_DIR / f"{stem}.json"
    if stem.startswith("B"):
        return _BOUNDARY_EXPECTED_DIR / f"{stem}.json"
    raise Week2RunError("Week 2 truth unavailable")


def _load_expected_results(
    cases: tuple[EvaluationCase, ...],
) -> dict[str, QueryResult]:
    expected: dict[str, QueryResult] = {}
    try:
        for case in cases:
            if case.expected_behavior != "execute":
                continue
            raw = json.loads(
                _expected_path(case, cases).read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
            expected[case.case_id] = QueryResult.model_validate_json(
                json.dumps(raw, ensure_ascii=False), strict=True
            )
    except Exception:
        raise Week2RunError("Week 2 truth unavailable") from None
    if len(expected) != 50:
        raise Week2RunError("Week 2 truth unavailable")
    return expected


def _oracle_body(path: Path) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise Week2RunError("Week 2 Oracle unavailable") from None
    parts = source.split("\n", 1)
    if len(parts) != 2 or not parts[0].startswith("--") or not parts[1].strip():
        raise Week2RunError("Week 2 Oracle unavailable")
    return parts[1]


def _fixture_mapping(
    cases: tuple[EvaluationCase, ...],
) -> Mapping[str, str]:
    return {
        case.case_id: _oracle_body(resolved_oracle_path(case, cases))
        for case in cases
        if case.expected_behavior == "execute"
    }


def _suite_manifest_sha256(
    cases: tuple[EvaluationCase, ...],
) -> str:
    files = set(_REGISTRY_PATHS)
    for case in cases:
        if case.expected_behavior == "execute":
            files.add(resolved_oracle_path(case, cases))
            files.add(_expected_path(case, cases))
    digest = sha256()
    try:
        for path in sorted(files, key=lambda item: item.relative_to(_REPOSITORY_ROOT).as_posix()):
            relative_bytes = path.relative_to(_REPOSITORY_ROOT).as_posix().encode("utf-8")
            contents = path.read_bytes()
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
    except (OSError, ValueError):
        raise Week2RunError("Week 2 suite manifest unavailable") from None
    return digest.hexdigest()


def _excluded_implementation_path(relative: Path) -> bool:
    lowered_parts = tuple(part.casefold() for part in relative.parts)
    return (
        any(part in _IMPLEMENTATION_EXCLUDED_PARTS for part in lowered_parts)
        or relative.name.casefold().startswith(".env")
        or relative.suffix.casefold() in _IMPLEMENTATION_EXCLUDED_SUFFIXES
    )


def _implementation_sha256() -> str:
    """Hash the repository inputs that can affect a Week 2 evaluation."""
    files: list[Path] = []
    try:
        for directory_name in _IMPLEMENTATION_DIRECTORIES:
            directory = _REPOSITORY_ROOT / directory_name
            if not directory.exists():
                if directory_name in _REQUIRED_IMPLEMENTATION_DIRECTORIES:
                    raise OSError
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise OSError
            for candidate in directory.rglob("*"):
                relative = candidate.relative_to(_REPOSITORY_ROOT)
                if _excluded_implementation_path(relative):
                    continue
                if candidate.is_symlink():
                    raise OSError
                if candidate.is_dir():
                    continue
                if not candidate.is_file():
                    raise OSError
                files.append(candidate)

        for filename in _IMPLEMENTATION_ROOT_FILES:
            candidate = _REPOSITORY_ROOT / filename
            if not candidate.exists():
                if filename in _REQUIRED_IMPLEMENTATION_ROOT_FILES:
                    raise OSError
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError
            files.append(candidate)

        if not files:
            raise OSError
        digest = sha256()
        for path in sorted(files, key=lambda item: item.relative_to(_REPOSITORY_ROOT).as_posix()):
            relative_bytes = path.relative_to(_REPOSITORY_ROOT).as_posix().encode("utf-8")
            contents = path.read_bytes()
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
    except (OSError, ValueError):
        raise Week2RunError("Week 2 implementation manifest unavailable") from None
    return digest.hexdigest()


def _provider_model_has_pricing(provider_model: str, pricing: ModelPricing) -> bool:
    return provider_model in {pricing.requested_model, pricing.resolved_model}


def _adapter_error_has_complete_usage(error: ModelAdapterError) -> bool:
    # The adapter emits two strictly positive counts only after validating both usage fields.
    return (
        type(error.input_tokens) is int
        and error.input_tokens > 0
        and type(error.output_tokens) is int
        and error.output_tokens > 0
    )


def _validated_live_pricing(
    pricing: ModelPricing, generator_model: str
) -> tuple[ModelPricing, str]:
    try:
        validated = ModelPricing.model_validate_json(pricing.model_dump_json(), strict=True)
        if (
            validated.requested_model != generator_model
            or not _safe_model_identifier(generator_model)
            or not _safe_model_identifier(validated.requested_model)
            or not _safe_model_identifier(validated.resolved_model)
            or validated.pricing_basis != WEEK2_PRICING_BASIS
            or WEEK2_FIXTURE_MODEL
            in {generator_model, validated.requested_model, validated.resolved_model}
        ):
            raise ValueError
        snapshot_digest = pricing_snapshot_sha256(validated)
    except Exception:
        raise Week2RunError("live Week 2 pricing metadata unavailable") from None
    return validated, snapshot_digest


def _reference_cost_matches_pricing(reference: BaselineRunReport, pricing: ModelPricing) -> bool:
    try:
        repriced_cases = tuple(
            estimate_cost_cny(case.input_tokens, case.output_tokens, pricing)
            for case in reference.cases
        )
    except Exception:
        return False
    return (
        all(
            repriced == case.estimated_cost_cny
            for repriced, case in zip(repriced_cases, reference.cases, strict=True)
        )
        and sum(repriced_cases, Decimal("0")) == reference.total_cost_cny
    )


def _validate_preflight_report_metadata(
    *,
    run_id: str,
    prompt_version: str,
    dataset_id: str,
    reference: BaselineRunReport,
) -> str:
    try:
        reference_digest = reference_report_sha256(reference)
        ComparisonReference(
            reference_run_id=run_id,
            reference_protocol=prompt_version,
            reference_dataset_id=dataset_id,
            reference_report_sha256=reference_digest,
            comparison_status="not_comparable",
        )
        ComparisonReference(
            reference_run_id=reference.run_id,
            reference_protocol=reference.prompt_version,
            reference_dataset_id=reference.dataset_id,
            reference_report_sha256=reference_digest,
            comparison_status="not_comparable",
        )
    except Exception:
        raise Week2RunError("Week 2 report metadata unavailable") from None
    return reference_digest


def _comparison_reference(
    reference: BaselineRunReport,
    *,
    mode: str,
    dataset_id: str,
    prompt_version: str,
    requested_model: str,
    resolved_models: tuple[str, ...],
    pricing: ModelPricing | None,
    cost_estimate_complete: bool,
    reference_report_digest: str,
) -> ComparisonReference:
    if reference_report_digest != reference_report_sha256(reference):
        raise ValueError("reference report digest does not match the parsed report")
    comparable = (
        mode == "live"
        and reference.mode == "live"
        and dataset_id == reference.dataset_id
        and prompt_version == reference.prompt_version
        and requested_model == reference.requested_model
        and resolved_models == reference.resolved_models
        and pricing is not None
        and pricing.effective_date == reference.pricing_effective_date
        and pricing.pricing_basis == reference.pricing_basis
        and cost_estimate_complete
        and _reference_cost_matches_pricing(reference, pricing)
    )
    return ComparisonReference(
        reference_run_id=reference.run_id,
        reference_protocol=reference.prompt_version,
        reference_dataset_id=reference.dataset_id,
        reference_report_sha256=reference_report_digest,
        comparison_status="comparable" if comparable else "not_comparable",
    )


async def execute_week2_sql(sql: str) -> Week2Execution:
    """Execute through the public governed tool and convert to evaluator rows."""
    response = await ExecuteSqlTool().run(ExecuteSqlRequest(sql=sql))
    if not response.ok or response.data is None:
        error = response.error
        if error is not None and error.code is ErrorCode.SQL_REJECTED:
            raise Week2SqlRejected("sql_rejected") from None
        category = error.code.value if error is not None else "execution_failed"
        raise Week2ExecutionError(category) from None
    return Week2Execution(
        query_id=response.data.query_id,
        result=QueryResult(columns=response.data.columns, rows=response.data.rows),
    )


def _safe_error_type(error: ModelAdapterError) -> str:
    category = str(error)
    return f"generation_{category}" if category in _MODEL_ERROR_CATEGORIES else "generation_failed"


def _generation_error_case(
    case: EvaluationCase,
    error: ModelAdapterError | None,
    *,
    cost: Decimal = Decimal("0"),
    error_type: str = "generation_failed",
) -> Week2CaseResult:
    return Week2CaseResult(
        case_id=case.case_id,
        suite=case.suite,
        status="invalid_sql",
        finish_reason=error.finish_reason if error is not None else None,
        output_truncated=error.output_truncated if error is not None else False,
        latency_ms=error.latency_ms if error is not None else 0,
        input_tokens=error.input_tokens if error is not None else 0,
        output_tokens=error.output_tokens if error is not None else 0,
        estimated_cost_cny=cost,
        error_type=error_type,
    )


def _execution_error_case(
    case: EvaluationCase,
    *,
    generated: GeneratedSql,
    cost: Decimal,
    status: Week2CaseStatus,
    error_type: str,
    query_id: str | None = None,
) -> Week2CaseResult:
    return Week2CaseResult(
        case_id=case.case_id,
        suite=case.suite,
        status=status,
        query_id=query_id,
        finish_reason=generated.finish_reason,
        output_truncated=generated.output_truncated,
        latency_ms=generated.latency_ms,
        input_tokens=generated.input_tokens,
        output_tokens=generated.output_tokens,
        estimated_cost_cny=cost,
        error_type=error_type,
    )


async def _run_week2_evaluation(
    mode: str = "fixture",
    output_root: str | Path = _DEFAULT_REPORT_ROOT,
    *,
    generator: EvaluationSqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    manifest_path: str | Path = _DEFAULT_MANIFEST_PATH,
    reference_path: str | Path = _DEFAULT_REFERENCE_PATH,
    executor: Week2Executor = execute_week2_sql,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
    _reservation_sink: Callable[[Week2ReportReservation], None],
) -> Week2RunReport:
    """Internal implementation; the public wrapper owns reservation cleanup."""
    if mode not in {"fixture", "live"}:
        raise Week2RunError("Week 2 mode unavailable")
    evaluation_mode = cast(Literal["fixture", "live"], mode)
    try:
        cases = load_active_suites()
        expected = _load_expected_results(cases)
        manifest = _load_manifest(manifest_path)
        reference = _load_reference(reference_path)
        suite_digest = _suite_manifest_sha256(cases)
        implementation_digest = _implementation_sha256()
        schema_context = build_schema_context()
        metric_context = build_metric_context()
        context_digest = context_sha256(schema_context, metric_context)
        prompt_version = (
            f"{BASELINE_PROMPT_VERSION}+{EVALUATION_CONTEXT_VERSION}+sha256:{context_digest}"
        )
        effective_run_id = uuid4().hex if run_id is None else run_id
        timestamp = (now or (lambda: datetime.now(UTC)))()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise Week2RunError("Week 2 report metadata unavailable")
        reference_digest = _validate_preflight_report_metadata(
            run_id=effective_run_id,
            prompt_version=prompt_version,
            dataset_id=manifest.dataset_id,
            reference=reference,
        )
    except Week2RunError:
        raise
    except Exception:
        raise Week2RunError("Week 2 inputs unavailable") from None

    if mode == "fixture":
        if generator is not None:
            raise Week2RunError("fixture generator injection is unavailable")
        try:
            active_generator: EvaluationSqlGenerator = Week2FixtureSqlGenerator(
                _fixture_mapping(cases)
            )
        except Exception:
            raise Week2RunError("Week 2 fixture generator unavailable") from None
        requested_model = WEEK2_FIXTURE_MODEL
        live_pricing: ModelPricing | None = None
        pricing_digest: str | None = None
    else:
        if (
            generator is None
            or not isinstance(generator, OpenAICompatibleSqlGenerator)
            or pricing is None
        ):
            raise Week2RunError("live Week 2 dependencies unavailable")
        try:
            generator_model = generator.model
        except Exception:
            raise Week2RunError("live Week 2 pricing metadata unavailable") from None
        live_pricing, pricing_digest = _validated_live_pricing(pricing, generator_model)
        active_generator = generator
        requested_model = generator_model

    try:
        report_reservation = reserve_week2_report(
            output_root,
            mode=evaluation_mode,
            run_id=effective_run_id,
            generated_at=timestamp,
        )
    except Exception:
        raise Week2RunError("Week 2 report publication unavailable") from None
    _reservation_sink(report_reservation)

    case_results: list[Week2CaseResult] = []
    resolved_models = {WEEK2_FIXTURE_MODEL} if mode == "fixture" else set()
    for case in cases:
        if case.expected_behavior == "reject":
            assert case.candidate_sql is not None
            assert case.expected_rejection is not None
            expected_rule = case.expected_rejection.value
            try:
                validate_sql(case.candidate_sql)
            except SqlPolicyError as error:
                actual_rule = error.code.value
                case_results.append(
                    Week2CaseResult(
                        case_id=case.case_id,
                        suite="safety",
                        status=(
                            "rejected" if actual_rule == expected_rule else "rejection_mismatch"
                        ),
                        error_type=(
                            None if actual_rule == expected_rule else "safety_rejection_mismatch"
                        ),
                        expected_rejection=expected_rule,
                        observed_rejection=actual_rule,
                    )
                )
            except Exception:
                case_results.append(
                    Week2CaseResult(
                        case_id=case.case_id,
                        suite="safety",
                        status="rejection_mismatch",
                        error_type="safety_rejection_mismatch",
                        expected_rejection=expected_rule,
                        observed_rejection="invalid_sql",
                    )
                )
            else:
                case_results.append(
                    Week2CaseResult(
                        case_id=case.case_id,
                        suite="safety",
                        status="unexpected_accept",
                        error_type="safety_unexpected_accept",
                        expected_rejection=expected_rule,
                    )
                )
            continue

        assert case.question is not None
        request = EvaluationGenerationRequest(
            case_id=case.case_id,
            question=case.question,
            schema_context=schema_context,
            metric_context=metric_context,
        )
        try:
            generated = await active_generator.generate(request)
        except ModelAdapterError as error:
            provider_model = error.provider_model
            provider_model_is_safe = provider_model is not None and _safe_model_identifier(
                provider_model
            )
            if provider_model_is_safe:
                assert provider_model is not None
                resolved_models.add(provider_model)
            error_cost = Decimal("0")
            error_type = _safe_error_type(error)
            if live_pricing is not None:
                if (
                    provider_model is None
                    or not provider_model_is_safe
                    or not _provider_model_has_pricing(provider_model, live_pricing)
                    or not _adapter_error_has_complete_usage(error)
                ):
                    error_type = "pricing_failed"
                else:
                    try:
                        error_cost = estimate_cost_cny(
                            error.input_tokens, error.output_tokens, live_pricing
                        )
                    except Exception:
                        error_type = "pricing_failed"
            case_results.append(
                _generation_error_case(case, error, cost=error_cost, error_type=error_type)
            )
            continue
        except Exception:
            case_results.append(
                _generation_error_case(
                    case,
                    None,
                    error_type=(
                        "pricing_failed" if live_pricing is not None else "generation_failed"
                    ),
                )
            )
            continue

        provider_model_is_safe = _safe_model_identifier(generated.provider_model)
        if provider_model_is_safe:
            resolved_models.add(generated.provider_model)
        cost = Decimal("0")
        if live_pricing is not None:
            if not provider_model_is_safe or not _provider_model_has_pricing(
                generated.provider_model, live_pricing
            ):
                case_results.append(
                    _execution_error_case(
                        case,
                        generated=generated,
                        cost=Decimal("0"),
                        status="invalid_sql",
                        error_type="pricing_failed",
                    )
                )
                continue
            try:
                cost = estimate_cost_cny(
                    generated.input_tokens, generated.output_tokens, live_pricing
                )
            except Exception:
                case_results.append(
                    _execution_error_case(
                        case,
                        generated=generated,
                        cost=Decimal("0"),
                        status="invalid_sql",
                        error_type="pricing_failed",
                    )
                )
                continue
        try:
            validated = validate_sql(generated.sql)
        except SqlPolicyError:
            case_results.append(
                _execution_error_case(
                    case,
                    generated=generated,
                    cost=cost,
                    status="invalid_sql",
                    error_type="sql_rejected",
                )
            )
            continue
        except Exception:
            case_results.append(
                _execution_error_case(
                    case,
                    generated=generated,
                    cost=cost,
                    status="invalid_sql",
                    error_type="sql_rejected",
                )
            )
            continue
        try:
            execution = await executor(validated.sql)
            if execution.query_id != validated.query_id:
                raise Week2ExecutionError("execution_failed")
        except Week2SqlRejected:
            case_results.append(
                _execution_error_case(
                    case,
                    generated=generated,
                    cost=cost,
                    status="execution_error",
                    error_type="execution_failed",
                    query_id=validated.query_id,
                )
            )
            continue
        except Week2ExecutionError as error:
            category = (
                error.category
                if error.category in {"query_timeout", "execution_failed"}
                else "execution_failed"
            )
            case_results.append(
                _execution_error_case(
                    case,
                    generated=generated,
                    cost=cost,
                    status="execution_error",
                    error_type=category,
                    query_id=validated.query_id,
                )
            )
            continue
        except Exception:
            case_results.append(
                _execution_error_case(
                    case,
                    generated=generated,
                    cost=cost,
                    status="execution_error",
                    error_type="execution_failed",
                    query_id=validated.query_id,
                )
            )
            continue

        try:
            assert case.comparison is not None
            score = score_result_and_contract(
                comparison=case.comparison,
                key_columns=case.key_columns,
                numeric_columns=case.numeric_columns,
                expected=expected[case.case_id],
                actual=execution.result,
            )
            status: Week2CaseStatus
            if score.result_score == Decimal("1"):
                status = "passed" if score.output_contract_conformant else "contract_violation"
            else:
                status = (
                    "wrong_answer"
                    if score.output_contract_conformant
                    else "wrong_answer_and_contract_violation"
                )
            case_results.append(
                Week2CaseResult(
                    case_id=case.case_id,
                    suite=case.suite,
                    status=status,
                    result_score=score.result_score,
                    output_contract_conformant=score.output_contract_conformant,
                    query_id=validated.query_id,
                    finish_reason=generated.finish_reason,
                    output_truncated=generated.output_truncated,
                    latency_ms=generated.latency_ms,
                    input_tokens=generated.input_tokens,
                    output_tokens=generated.output_tokens,
                    estimated_cost_cny=cost,
                )
            )
        except Exception:
            case_results.append(
                _execution_error_case(
                    case,
                    generated=generated,
                    cost=cost,
                    status="execution_error",
                    error_type="scoring_failed",
                    query_id=validated.query_id,
                )
            )

    try:
        derived = derive_week2_aggregates(tuple(case_results))
        comparison = _comparison_reference(
            reference,
            mode=evaluation_mode,
            dataset_id=manifest.dataset_id,
            prompt_version=prompt_version,
            requested_model=requested_model,
            resolved_models=tuple(sorted(resolved_models)),
            pricing=live_pricing,
            cost_estimate_complete=derived.cost_estimate_complete,
            reference_report_digest=reference_digest,
        )
        report = build_week2_run_report(
            case_results,
            run_id=effective_run_id,
            mode=evaluation_mode,
            suite_manifest_sha256=suite_digest,
            implementation_sha256=implementation_digest,
            dataset_id=manifest.dataset_id,
            prompt_version=prompt_version,
            requested_model=requested_model,
            resolved_models=tuple(sorted(resolved_models)),
            comparison_reference=comparison,
            pricing=live_pricing,
        )
        if report.pricing_snapshot_sha256 != pricing_digest:
            raise ValueError
        write_week2_report(
            report,
            generated_at=timestamp,
            reservation=report_reservation,
        )
    except Exception:
        raise Week2RunError("Week 2 report publication failed") from None
    return report


async def run_week2_evaluation(
    mode: str = "fixture",
    output_root: str | Path = _DEFAULT_REPORT_ROOT,
    *,
    generator: EvaluationSqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    manifest_path: str | Path = _DEFAULT_MANIFEST_PATH,
    reference_path: str | Path = _DEFAULT_REFERENCE_PATH,
    executor: Week2Executor = execute_week2_sql,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
) -> Week2RunReport:
    """Run each execute boundary once and validate safety cases without a model call."""
    reservations: list[Week2ReportReservation] = []
    try:
        return await _run_week2_evaluation(
            mode=mode,
            output_root=output_root,
            generator=generator,
            pricing=pricing,
            manifest_path=manifest_path,
            reference_path=reference_path,
            executor=executor,
            now=now,
            run_id=run_id,
            _reservation_sink=reservations.append,
        )
    finally:
        for reservation in reservations:
            cancel_week2_report_reservation(reservation)
