from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from governed_analytics.agent.contracts import (
    AgentRunResult,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    SafeTrace,
    StopReason,
)
from governed_analytics.evals.week3.runner import run_week3_evaluation
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
