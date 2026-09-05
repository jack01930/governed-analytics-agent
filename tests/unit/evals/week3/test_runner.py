from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    SafeTrace,
    StopReason,
    ToolCallTrace,
)
from governed_analytics.evals.week3 import runner
from governed_analytics.evals.week3.runner import _suite_score, run_week3_evaluation
from governed_analytics.evals.week3.suites import load_week3_cases


def _failure(case_id: str) -> AgentRunResult:
    return AgentRunResult(
        run_id=case_id,
        behavior=None,
        answer_contract=None,
        observations=(),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=None,
        repair_history=(),
        governance=GovernanceSnapshot(),
        final_answer=FinalAnswer(
            status=FinalStatus.INTERNAL_ERROR,
            stop_reason=StopReason.INTERNAL_ERROR,
            answer="受控失败。",
        ),
        safe_trace=SafeTrace(),
    )


@pytest.mark.asyncio
async def test_runner_passes_only_case_id_and_question_to_executor(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class SpyExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            calls.append((case_id, question))
            return _failure(case_id)

    cases = load_week3_cases()[:1]
    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=SpyExecutor(),
        cases=cases,
        output_root=tmp_path,
        run_id="week3-unit",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert calls == [(case.case_id, case.question) for case in cases]
    assert artifact.report.protocol_version == "week3-agent-evaluation-v1"
    assert artifact.report.report_scope == "partial_test"
    assert artifact.report_json.is_file()


@pytest.mark.asyncio
async def test_runner_sanitizes_case_exception_and_continues(tmp_path: Path) -> None:
    calls: list[str] = []

    class FailingFirstExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del question
            calls.append(case_id)
            if len(calls) == 1:
                raise RuntimeError("select SECRET_ROW from https://endpoint.invalid sk-secret")
            return _failure(case_id)

    cases = load_week3_cases()[:2]
    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=FailingFirstExecutor(),
        cases=cases,
        output_root=tmp_path,
        run_id="continue",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert calls == [case.case_id for case in cases]
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in artifact.report_dir.rglob("*")
        if path.is_file()
    ).casefold()
    assert "secret_row" not in combined
    assert "endpoint.invalid" not in combined
    assert "sk-secret" not in combined


@pytest.mark.asyncio
async def test_runner_propagates_cancellation_and_cleans_its_reservation(
    tmp_path: Path,
) -> None:
    class CancelledExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del case_id, question
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_week3_evaluation(
            mode="fixture",
            executor=CancelledExecutor(),
            cases=load_week3_cases()[:1],
            output_root=tmp_path,
            run_id="cancelled",
            now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert not tuple((tmp_path / "fixture").iterdir())


@pytest.mark.asyncio
async def test_runner_enforces_mode_pricing_contract_before_reservation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="pricing"):
        await run_week3_evaluation(
            mode="live",
            executor=object(),  # type: ignore[arg-type]
            cases=load_week3_cases()[:1],
            output_root=tmp_path,
        )
    assert not tuple(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_runner_rejects_known_result_replayed_for_heldout_case(tmp_path: Path) -> None:
    case = load_week3_cases()[-1:]

    class ReplayingExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del case_id, question
            return _failure("W3K001")

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=ReplayingExecutor(),
        cases=case,
        output_root=tmp_path,
        run_id="replay",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert artifact.report.cases[0].error_type == "case_identity_mismatch"
    assert not artifact.report.cases[0].passed


@pytest.mark.asyncio
async def test_runner_freezes_expected_results_before_invoking_any_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    class SpyExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            nonlocal called
            del case_id, question
            called = True
            raise AssertionError("executor must not run")

    def unavailable_expected(_path: Path) -> object:
        raise RuntimeError("private expected diagnostic")

    monkeypatch.setattr(runner, "_read_expected", unavailable_expected)
    with pytest.raises(RuntimeError, match="private expected diagnostic"):
        await run_week3_evaluation(
            mode="fixture",
            executor=SpyExecutor(),
            cases=(load_week3_cases()[10],),
            output_root=tmp_path,
            run_id="expected-freeze",
            now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert not called
    assert not tuple((tmp_path / "fixture").iterdir())


@pytest.mark.asyncio
async def test_missing_validation_becomes_explicit_case_failure_not_run_failure(
    tmp_path: Path,
) -> None:
    case = load_week3_cases()[10]

    class MissingValidationExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del question
            trace = (
                ToolCallTrace(
                    tool_name=ActionType.METRIC_LOOKUP,
                    purpose="metric_lookup",
                    safe_arguments=(),
                ),
                ToolCallTrace(
                    tool_name=ActionType.SCHEMA_LOOKUP,
                    purpose="schema_lookup",
                    safe_arguments=(),
                ),
                ToolCallTrace(
                    tool_name=ActionType.EXECUTE_SQL,
                    purpose="metric_value_contract",
                    safe_arguments=(
                        ("contract_id", "metric_value_contract"),
                        ("hypothesis_id", "metric_value"),
                    ),
                    query_id="a" * 64,
                    columns=("gmv",),
                    row_count=1,
                ),
            )
            return _failure(case_id).model_copy(
                update={
                    "governance": GovernanceSnapshot(tool_calls=3, execute_calls=1),
                    "safe_trace": SafeTrace(tool_calls=trace),
                }
            )

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=MissingValidationExecutor(),
        cases=(case,),
        output_root=tmp_path,
        run_id="missing-validation",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert artifact.report_json.is_file()
    assert artifact.report.cases[0].error_type == "scoring_contract_failure"
    assert artifact.report.cases[0].valid_execute_count == 0


@pytest.mark.parametrize(
    ("diagnostic", "conformant"),
    (("read_only_policy", True), ("sql_timeout", False), ("database_error", False)),
)
def test_policy_suite_accepts_only_exact_read_only_rejection(
    diagnostic: str, conformant: bool
) -> None:
    case = load_week3_cases()[29]
    trace = ToolCallTrace(
        tool_name=ActionType.EXECUTE_SQL,
        purpose="metric_value_contract",
        safe_arguments=(
            ("contract_id", "metric_value_contract"),
            ("hypothesis_id", "metric_value"),
        ),
        safe_error=diagnostic,
    )
    result = AgentRunResult.model_construct(
        observations=(),
        observation_validations=(),
        evidence=(),
        governance=GovernanceSnapshot(),
        safe_trace=SafeTrace(tool_calls=(trace,)),
    )

    assert _suite_score(case, result).conformant is conformant


@pytest.mark.asyncio
async def test_hard_cost_exceed_preserves_safe_usage_in_nonconformant_result(
    tmp_path: Path,
) -> None:
    class CostExceededExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del question
            return _failure(case_id).model_copy(
                update={
                    "governance": GovernanceSnapshot(
                        committed_cost_cny=Decimal("0.31"),
                        input_tokens=123,
                        output_tokens=45,
                    )
                }
            )

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=CostExceededExecutor(),
        cases=load_week3_cases()[:1],
        output_root=tmp_path,
        run_id="hard-cost",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    result = artifact.report.cases[0]

    assert result.budget_score.committed_cost_cny == Decimal("0.31")
    assert result.budget_score.input_tokens == 123
    assert result.budget_score.output_tokens == 45
    assert not result.budget_conformant
    assert result.error_type != "scoring_contract_failure"
