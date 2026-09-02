"""One-pass, fixture-first baseline execution with no model-client construction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from governed_analytics.data_generation.models import DatasetManifest, DatasetScale
from governed_analytics.evals.context import (
    EVALUATION_CONTEXT_VERSION,
    build_metric_context,
    build_schema_context,
)
from governed_analytics.evals.executor import execute_readonly_sql
from governed_analytics.evals.golden import load_golden_cases
from governed_analytics.evals.models import BaselineCaseResult, BaselineRunReport, QueryResult
from governed_analytics.evals.pricing import ModelPricing, estimate_cost_cny
from governed_analytics.evals.reporting import build_baseline_run_report, write_baseline_report
from governed_analytics.evals.scorers import score_result
from governed_analytics.evals.sql_guard import SqlRejected
from governed_analytics.models.fixtures import FixtureSqlGenerator
from governed_analytics.models.openai_compatible import (
    ModelAdapterError,
    OpenAICompatibleSqlGenerator,
)
from governed_analytics.models.prompts import BASELINE_PROMPT_VERSION
from governed_analytics.models.protocols import SqlGenerationRequest, SqlGenerator

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CASES_PATH = "evals/datasets/golden/cases.yaml"
_DEFAULT_EXPECTED_DIR = _REPOSITORY_ROOT / "evals" / "datasets" / "golden" / "expected"
_DEFAULT_FIXTURE_PATH = "evals/fixtures/baseline_sql.json"
_DEFAULT_MANIFEST_PATH = (
    _REPOSITORY_ROOT / "artifacts" / "datasets" / "tiny" / "dataset_manifest.json"
)
_DEFAULT_REPORT_ROOT = _REPOSITORY_ROOT / "artifacts" / "evals" / "baseline"
_EXPECTED_CASE_IDS = tuple(f"G{number:03d}" for number in range(1, 21))
_MODEL_ERROR_CATEGORIES = frozenset(
    {
        "invalid_request",
        "provider_call_failed",
        "missing_content",
        "invalid_content",
        "missing_usage",
        "invalid_usage",
        "missing_model",
    }
)

type Executor = Callable[[str], Awaitable[QueryResult]]


class BaselineRunError(ValueError):
    """Sanitized global failure: no completed report has been published."""


def _resolve_from_repository(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def _load_expected_results(path: str | Path) -> dict[str, QueryResult]:
    directory = _resolve_from_repository(path)
    results: dict[str, QueryResult] = {}
    try:
        if tuple(sorted(item.name for item in directory.glob("*.json"))) != tuple(
            f"{case_id}.json" for case_id in _EXPECTED_CASE_IDS
        ):
            raise ValueError
        for case_id in _EXPECTED_CASE_IDS:
            result_path = directory / f"{case_id}.json"
            results[case_id] = QueryResult.model_validate_json(
                result_path.read_text(encoding="utf-8"), strict=True
            )
    except Exception:
        raise BaselineRunError("baseline truth unavailable") from None
    if tuple(results) != _EXPECTED_CASE_IDS:
        raise BaselineRunError("baseline truth unavailable")
    return results


def _load_tiny_manifest(path: str | Path) -> DatasetManifest:
    try:
        manifest = DatasetManifest.model_validate_json(
            _resolve_from_repository(path).read_text(encoding="utf-8"), strict=True
        )
    except Exception:
        raise BaselineRunError("tiny dataset manifest unavailable") from None
    if manifest.scale is not DatasetScale.TINY:
        raise BaselineRunError("tiny dataset manifest unavailable")
    return manifest


def _error_case(
    case_id: str,
    *,
    generated_sql: str | None,
    latency_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: Decimal = Decimal("0"),
    status: str,
    error_type: str,
) -> BaselineCaseResult:
    return BaselineCaseResult(
        case_id=case_id,
        generated_sql=generated_sql,
        status=status,  # type: ignore[arg-type]
        score=Decimal("0"),
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_cny=cost,
        error_type=error_type,
    )


async def run_baseline(
    mode: str = "fixture",
    output_root: str | Path = _DEFAULT_REPORT_ROOT,
    *,
    generator: SqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    fixture_path: str | Path = _DEFAULT_FIXTURE_PATH,
    cases_path: str | Path = _DEFAULT_CASES_PATH,
    expected_dir: str | Path = _DEFAULT_EXPECTED_DIR,
    manifest_path: str | Path = _DEFAULT_MANIFEST_PATH,
    executor: Executor = execute_readonly_sql,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
) -> BaselineRunReport:
    """Run exactly one generation and one execution attempt for each golden case."""
    if mode not in {"fixture", "live"}:
        raise BaselineRunError("baseline mode unavailable")
    try:
        cases = load_golden_cases(cases_path)
        if tuple(case.case_id for case in cases) != _EXPECTED_CASE_IDS:
            raise ValueError
        expected = _load_expected_results(expected_dir)
        manifest = _load_tiny_manifest(manifest_path)
        schema_context = build_schema_context()
        metric_context = build_metric_context()
    except BaselineRunError:
        raise
    except Exception:
        raise BaselineRunError("baseline inputs unavailable") from None

    if mode == "fixture":
        if generator is not None:
            raise BaselineRunError("fixture generator injection is unavailable")
        try:
            active_generator: SqlGenerator = FixtureSqlGenerator.from_path(fixture_path)
        except Exception:
            raise BaselineRunError("fixture generator unavailable") from None
        requested_model = "fixture-oracle"
        live_pricing: ModelPricing | None = None
    else:
        if (
            generator is None
            or not isinstance(generator, OpenAICompatibleSqlGenerator)
            or pricing is None
        ):
            raise BaselineRunError("live baseline dependencies unavailable")
        active_generator = generator
        requested_model = generator.model
        assert pricing is not None
        live_pricing = pricing

    case_results: list[BaselineCaseResult] = []
    resolved_models: set[str] = set()
    for case in cases:
        request = SqlGenerationRequest(
            case_id=case.case_id,
            question=case.question,
            schema_context=schema_context,
            metric_context=metric_context,
        )
        try:
            generated = await active_generator.generate(request)
        except ModelAdapterError as error:
            category = str(error)
            error_type = (
                f"generation_{category}"
                if category in _MODEL_ERROR_CATEGORIES
                else "generation_failed"
            )
            case_results.append(
                _error_case(
                    case.case_id,
                    generated_sql=None,
                    status="invalid_sql",
                    error_type=error_type,
                )
            )
            continue
        except Exception:
            case_results.append(
                _error_case(
                    case.case_id,
                    generated_sql=None,
                    status="invalid_sql",
                    error_type="generation_failed",
                )
            )
            continue
        resolved_models.add(generated.provider_model)
        cost = Decimal("0")
        if mode == "live":
            try:
                if live_pricing is None:
                    raise RuntimeError
                cost = estimate_cost_cny(
                    generated.input_tokens, generated.output_tokens, live_pricing
                )
            except Exception:
                case_results.append(
                    _error_case(
                        case.case_id,
                        generated_sql=generated.sql,
                        latency_ms=generated.latency_ms,
                        input_tokens=generated.input_tokens,
                        output_tokens=generated.output_tokens,
                        status="invalid_sql",
                        error_type="pricing_failed",
                    )
                )
                continue
        try:
            actual = await executor(generated.sql)
        except SqlRejected:
            case_results.append(
                _error_case(
                    case.case_id,
                    generated_sql=generated.sql,
                    latency_ms=generated.latency_ms,
                    input_tokens=generated.input_tokens,
                    output_tokens=generated.output_tokens,
                    cost=cost,
                    status="invalid_sql",
                    error_type="sql_rejected",
                )
            )
            continue
        except Exception:
            case_results.append(
                _error_case(
                    case.case_id,
                    generated_sql=generated.sql,
                    latency_ms=generated.latency_ms,
                    input_tokens=generated.input_tokens,
                    output_tokens=generated.output_tokens,
                    cost=cost,
                    status="execution_error",
                    error_type="execution_failed",
                )
            )
            continue
        score = score_result(case, expected[case.case_id], actual)
        case_results.append(
            BaselineCaseResult(
                case_id=case.case_id,
                generated_sql=generated.sql,
                status="passed" if score == Decimal("1") else "wrong_answer",
                score=score,
                latency_ms=generated.latency_ms,
                input_tokens=generated.input_tokens,
                output_tokens=generated.output_tokens,
                estimated_cost_cny=cost,
            )
        )
    try:
        report = build_baseline_run_report(
            case_results,
            run_id=run_id or uuid4().hex,
            mode=mode,  # type: ignore[arg-type]
            dataset_id=manifest.dataset_id,
            model=requested_model,
            prompt_version=f"{BASELINE_PROMPT_VERSION}+{EVALUATION_CONTEXT_VERSION}",
            requested_model=requested_model,
            resolved_models=tuple(sorted(resolved_models)),
        )
        timestamp = (now or (lambda: datetime.now(UTC)))()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        write_baseline_report(report, output_root, generated_at=timestamp)
    except Exception:
        raise BaselineRunError("baseline report publication failed") from None
    return report
