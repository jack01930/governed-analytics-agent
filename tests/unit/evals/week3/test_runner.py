from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from governed_analytics.agent.contracts import (
    ActionType,
    AgentFinishReason,
    AgentRunResult,
    AnswerContract,
    BehaviorAction,
    BehaviorDecision,
    BehaviorReasonCode,
    ColumnContract,
    EvidenceItem,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    ModelCallTrace,
    NodeTrace,
    Observation,
    ObservationContract,
    ObservationValidation,
    ResultShape,
    SafeTrace,
    StopReason,
    ToolCallTrace,
)
from governed_analytics.agent.ports import AgentModel, AgentTools
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.week3 import runner
from governed_analytics.evals.week3.models import Week3RunReport
from governed_analytics.evals.week3.runner import (
    FixtureWeek3CaseExecutor,
    LiveWeek3CaseExecutor,
    _score_case,
    _suite_score,
    run_week3_evaluation,
)
from governed_analytics.evals.week3.suites import load_week3_cases
from governed_analytics.pricing import load_model_pricing


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


def _simple_result_with_evidence_value(
    value: Decimal, *, provider_model: str = "fixture-agent"
) -> AgentRunResult:
    query_id = "b" * 64
    actual = Decimal("94636.23")
    observation = Observation(
        observation_id="observation-1",
        tool_name=ActionType.EXECUTE_SQL,
        purpose="metric_value_contract",
        ok=True,
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        query_id=query_id,
        columns=("gmv",),
        row_count=1,
        payload={
            "query_id": query_id,
            "columns": ("gmv",),
            "rows": ((str(actual),),),
            "row_count": 1,
            "row_limit": 500,
            "possibly_truncated": False,
        },
    )
    validation = ObservationValidation(
        observation_id=observation.observation_id,
        contract_id="metric_value_contract",
        validation_fingerprint="c" * 64,
        valid=True,
    )
    evidence = EvidenceItem(
        evidence_id="evidence-1",
        observation_id=observation.observation_id,
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        query_id=query_id,
        claim_key="gmv",
        stance="supports",
        numeric_value=value,
        unit="cny",
        verified=True,
    )
    answer_contract = AnswerContract(
        answer_contract_id="simple-answer",
        required_hypotheses=("metric_value",),
        observation_contracts=(
            ObservationContract(
                contract_id="metric_value_contract",
                hypothesis_id="metric_value",
                columns=(
                    ColumnContract(name="gmv", data_type="decimal", role="metric", unit="cny"),
                ),
                shape=ResultShape.SCALAR,
                min_rows=1,
                max_rows=1,
            ),
        ),
    )
    tool_calls = (
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
            query_id=query_id,
            columns=("gmv",),
            row_count=1,
        ),
    )
    model_calls = tuple(
        ModelCallTrace(
            purpose=purpose,
            provider_model=provider_model,
            outcome="completed",
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            finish_reason=AgentFinishReason.STOP,
            output_truncated=False,
            estimated_cost_cny=Decimal("0"),
        )
        for purpose in ("behavior", "plan", "action", "synthesis")
    )
    return AgentRunResult(
        run_id="W3K011",
        behavior=BehaviorDecision(
            action=BehaviorAction.EXECUTE,
            reason_code=BehaviorReasonCode.READY,
            user_message="开始分析。",
        ),
        answer_contract=answer_contract,
        observations=(observation,),
        observation_validations=(validation,),
        evidence=(evidence,),
        evidence_gaps=(),
        first_candidate=observation,
        repair_history=(),
        governance=GovernanceSnapshot(action_loops=1, llm_calls=4, tool_calls=3, execute_calls=1),
        final_answer=FinalAnswer(
            status=FinalStatus.COMPLETED,
            stop_reason=StopReason.ANSWER_COMPLETE,
            answer=f"GMV 是 {value} CNY。",
            evidence_ids=(evidence.evidence_id,),
            result_summary={
                "evidence": ({"evidence_id": evidence.evidence_id, "numeric_value": str(value)},)
            },
        ),
        safe_trace=SafeTrace(model_calls=model_calls, tool_calls=tool_calls),
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


@pytest.mark.parametrize("provider_model", ("deepseek-v4-flash", "DeepSeek-V4-Flash-0731"))
@pytest.mark.asyncio
async def test_live_pricing_bound_identity_is_preserved_in_partial_report(
    tmp_path: Path, provider_model: str
) -> None:
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")

    class AliasExecutor:
        settings = AgentRuntimeSettings.model_validate({})

        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del case_id, question
            return _simple_result_with_evidence_value(
                Decimal("94636.23"), provider_model=provider_model
            )

    case = next(item for item in load_week3_cases() if item.case_id == "W3K011")
    artifact = await run_week3_evaluation(
        mode="live",
        executor=AliasExecutor(),
        cases=(case,),
        pricing=pricing,
        output_root=tmp_path,
        run_id=f"live-{provider_model}",
        now=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    result = artifact.report.cases[0]
    assert artifact.report.report_scope == "partial_test"
    assert artifact.report.resolved_models == (provider_model,)
    assert result.expected_resolved_model == provider_model
    assert result.resolved_models == (provider_model,)
    assert result.model_identity_complete
    assert artifact.report_json.is_file()


@pytest.mark.asyncio
async def test_live_unknown_model_fails_identity_but_still_publishes_partial_report(
    tmp_path: Path,
) -> None:
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")

    class UnknownExecutor:
        settings = AgentRuntimeSettings.model_validate({})

        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del case_id, question
            return _simple_result_with_evidence_value(
                Decimal("94636.23"), provider_model="deepseek-v4-flash-unknown"
            )

    case = next(item for item in load_week3_cases() if item.case_id == "W3K011")
    artifact = await run_week3_evaluation(
        mode="live",
        executor=UnknownExecutor(),
        cases=(case,),
        pricing=pricing,
        output_root=tmp_path,
        run_id="live-unknown",
        now=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    result = artifact.report.cases[0]
    assert artifact.report.resolved_models == ("deepseek-v4-flash-unknown",)
    assert result.expected_resolved_model == "deepseek-v4-flash"
    assert result.resolved_models == ("deepseek-v4-flash-unknown",)
    assert not result.model_identity_complete
    assert not result.passed
    assert artifact.report_json.is_file()


@pytest.mark.asyncio
async def test_live_mixed_pricing_identities_are_preserved_but_not_reported_complete(
    tmp_path: Path,
) -> None:
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")

    class MixedExecutor:
        settings = AgentRuntimeSettings.model_validate({})

        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            del case_id, question
            result = _simple_result_with_evidence_value(Decimal("94636.23"))
            calls = tuple(
                call.model_copy(
                    update={
                        "provider_model": (
                            pricing.requested_model if index % 2 == 0 else pricing.resolved_model
                        )
                    }
                )
                for index, call in enumerate(result.safe_trace.model_calls)
            )
            return result.model_copy(
                update={"safe_trace": result.safe_trace.model_copy(update={"model_calls": calls})}
            )

    case = next(item for item in load_week3_cases() if item.case_id == "W3K011")
    artifact = await run_week3_evaluation(
        mode="live",
        executor=MixedExecutor(),
        cases=(case,),
        pricing=pricing,
        output_root=tmp_path,
        run_id="live-mixed",
        now=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    result = artifact.report.cases[0]
    assert artifact.report.resolved_models == (
        pricing.requested_model,
        pricing.resolved_model,
    )
    assert result.expected_resolved_model == pricing.requested_model
    assert not result.model_identity_complete
    assert not result.passed
    assert artifact.report_json.is_file()


@pytest.mark.parametrize(
    ("provider_model", "accepted"),
    (
        ("deepseek-v4-flash", True),
        ("DeepSeek-V4-Flash-0731", True),
        ("deepseek-v4-flash-unknown", False),
    ),
)
@pytest.mark.asyncio
async def test_live_executor_accepts_only_exact_pricing_bound_identities(
    provider_model: str,
    accepted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pricing = load_model_pricing("data/pricing/deepseek-v4-flash-2026-09-01.yaml")

    class Model:
        model = pricing.requested_model

    executor = LiveWeek3CaseExecutor(
        model=cast(AgentModel, Model()),
        tools=cast(AgentTools, object()),
        settings=AgentRuntimeSettings.model_validate({}),
        pricing=pricing,
    )
    observed = _simple_result_with_evidence_value(
        Decimal("94636.23"), provider_model=provider_model
    )

    async def execute(**_kwargs: object) -> AgentRunResult:
        return observed

    monkeypatch.setattr(executor, "_execute", execute)
    result = await executor.run_case(case_id="W3K011", question="June GMV?")

    assert (result.final_answer.status is FinalStatus.COMPLETED) is accepted
    assert result.safe_trace.model_calls[0].provider_model == provider_model


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

    def unavailable_snapshot() -> object:
        raise RuntimeError("private expected diagnostic")

    monkeypatch.setattr(runner, "_load_protocol_snapshot", unavailable_snapshot)
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
    assert not tuple(tmp_path.iterdir())


def test_wrong_evidence_numeric_claim_cannot_keep_w3k011_green() -> None:
    case = next(item for item in load_week3_cases() if item.case_id == "W3K011")
    scored = _score_case(
        case,
        _simple_result_with_evidence_value(Decimal("999999")),
        AgentRuntimeSettings.model_validate({}),
        expected_results=(QueryResult(columns=("gmv",), rows=((Decimal("94636.23"),),)),),
        expected_resolved_model="fixture-agent",
    )

    assert scored.final_candidate_score is not None
    assert scored.final_candidate_score.strict_pass
    assert scored.evidence_score is not None
    assert not scored.evidence_score.oracle_verified_sufficient
    assert not scored.passed


@pytest.mark.asyncio
async def test_fixture_execution_uses_the_handle_bound_snapshot_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = runner.__dict__["_load_protocol_snapshot"]()
    executed: list[str] = []

    class CapturingExecutor(FixtureWeek3CaseExecutor):
        async def _execute(self, **kwargs: object) -> AgentRunResult:
            case_id = cast(str, kwargs["case_id"])
            executed.append(case_id)
            return _failure(case_id)

    def stale_path_loader() -> object:
        raise AssertionError("fixture execution must not reload scripts by path")

    executor = CapturingExecutor(
        tools=cast(AgentTools, object()),
        settings=AgentRuntimeSettings.model_validate({}),
    )
    monkeypatch.setattr(runner, "_load_protocol_snapshot", lambda: snapshot)
    monkeypatch.setattr(runner, "load_fixture_scripts", stale_path_loader, raising=False)

    await run_week3_evaluation(
        mode="fixture",
        executor=executor,
        cases=(snapshot.cases[0],),
        output_root=tmp_path,
        run_id="snapshot-catalog",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert executed == ["W3K001"]


@pytest.mark.asyncio
async def test_runner_rejects_expected_leaf_symlink_before_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = tmp_path / "week3"
    shutil.copytree(cast(Path, runner.__dict__["WEEK3_ROOT"]), protocol)
    expected = protocol / "expected/W3K011.json"
    owned = protocol / "expected/W3K011-owned.json"
    expected.rename(owned)
    expected.symlink_to(owned.name)
    calls = 0

    class SpyExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            nonlocal calls
            del case_id, question
            calls += 1
            return _failure("W3K011")

    monkeypatch.setattr(runner, "WEEK3_ROOT", protocol, raising=False)
    with pytest.raises(ValueError, match="protocol snapshot"):
        await run_week3_evaluation(
            mode="fixture",
            executor=SpyExecutor(),
            output_root=tmp_path / "output",
            run_id="expected-leaf-symlink",
            now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_runner_rejects_protocol_parent_component_symlink_before_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    protocol = real_parent / "week3"
    shutil.copytree(cast(Path, runner.__dict__["WEEK3_ROOT"]), protocol)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    calls = 0

    class SpyExecutor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            nonlocal calls
            del case_id, question
            calls += 1
            return _failure("W3K001")

    monkeypatch.setattr(runner, "WEEK3_ROOT", linked_parent / "week3")
    with pytest.raises(ValueError, match="protocol snapshot"):
        await run_week3_evaluation(
            mode="fixture",
            executor=SpyExecutor(),
            output_root=tmp_path / "output",
        )

    assert calls == 0


@pytest.mark.parametrize("phase", ("before_open", "after_identity", "after_read"))
def test_protocol_snapshot_rejects_restored_source_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    protocol = tmp_path / "week3"
    shutil.copytree(cast(Path, runner.__dict__["WEEK3_ROOT"]), protocol)
    target = protocol / "expected/W3K011.json"
    target_inode = target.stat().st_ino
    monkeypatch.setattr(runner, "WEEK3_ROOT", protocol)

    if phase in {"before_open", "after_read"}:
        original = cast(Callable[[int, str], bytes], runner.__dict__["_read_protocol_file"])
        injected = False

        def swap_around_read(directory_fd: int, name: str) -> bytes:
            nonlocal injected
            if name != target.name or injected:
                return original(directory_fd, name)
            injected = True
            if phase == "before_open":
                os.rename(name, "owned.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                source_fd = os.open("owned.json", os.O_RDONLY, dir_fd=directory_fd)
                replacement_fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(replacement_fd, os.pread(source_fd, 1 << 20, 0))
                finally:
                    os.close(source_fd)
                    os.close(replacement_fd)
                content = original(directory_fd, name)
                os.unlink(name, dir_fd=directory_fd)
                os.rename("owned.json", name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                return content
            content = original(directory_fd, name)
            os.rename(name, "owned.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.rename("owned.json", name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            return content

        monkeypatch.setattr(runner, "_read_protocol_file", swap_around_read)
    else:
        original_pread = cast(Callable[[int], bytes], runner.__dict__["_pread_all"])
        injected = False

        def swap_after_identity(fd: int) -> bytes:
            nonlocal injected
            if os.fstat(fd).st_ino == target_inode and not injected:
                injected = True
                target.rename(target.with_name("owned.json"))
                target.write_bytes(target.with_name("owned.json").read_bytes())
                content = original_pread(fd)
                target.unlink()
                target.with_name("owned.json").rename(target)
                return content
            return original_pread(fd)

        monkeypatch.setattr(runner, "_pread_all", swap_after_identity)

    with pytest.raises(ValueError, match="protocol snapshot"):
        runner.__dict__["_load_protocol_snapshot"]()


def test_protocol_snapshot_rejects_same_inode_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = tmp_path / "week3"
    shutil.copytree(cast(Path, runner.__dict__["WEEK3_ROOT"]), protocol)
    target = protocol / "expected/W3K011.json"
    target_inode = target.stat().st_ino
    original_pread = cast(Callable[[int], bytes], runner.__dict__["_pread_all"])
    tampered = False

    def tamper_during_read(fd: int) -> bytes:
        nonlocal tampered
        if os.fstat(fd).st_ino == target_inode and not tampered:
            tampered = True
            write_fd = os.open(target, os.O_WRONLY)
            try:
                os.pwrite(write_fd, b"X", 0)
                os.fsync(write_fd)
            finally:
                os.close(write_fd)
        return original_pread(fd)

    monkeypatch.setattr(runner, "WEEK3_ROOT", protocol)
    monkeypatch.setattr(runner, "_pread_all", tamper_during_read)
    with pytest.raises(ValueError, match="protocol snapshot"):
        runner.__dict__["_load_protocol_snapshot"]()


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
    result = artifact.report.cases[0]
    assert result.valid_execute_count == 0
    assert result.observed_tool_calls == 3
    assert result.observed_execute_calls == 1
    assert len(result.safe_tool_trace) == 3
    assert result.scoring_failure is not None
    assert result.scoring_failure.stage == "report_validation"
    assert result.scoring_failure.diagnostic_sha256 is not None
    assert result.scoring_failure.validation_codes == ("execute_validation_linkage",)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total", "structured", "conformant"),
    ((1, 1, True), (1, 0, False), (2, 2, False), (2, 1, False)),
)
async def test_actual_repairs_are_publishable_and_scored_by_kind(
    tmp_path: Path,
    total: int,
    structured: int,
    conformant: bool,
) -> None:
    outcome = _simple_result_with_evidence_value(Decimal("94636.23"))
    outcome = outcome.model_copy(
        update={
            "governance": outcome.governance.model_copy(
                update={
                    "repair_count": total,
                    "structured_output_repair_count": structured,
                }
            )
        }
    )

    class Executor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            return outcome

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=Executor(),
        cases=(load_week3_cases()[10],),
        output_root=tmp_path,
        run_id="repair-facts",
    )
    case = artifact.report.cases[0]
    assert case.error_type is None
    assert case.scoring_failure is None
    assert case.budget_score.repair_count == total
    assert case.budget_score.structured_output_repair_count == structured
    assert case.budget_score.result_contract_repair_count == total - structured
    assert case.budget_conformant is conformant
    assert case.passed is conformant
    assert case.safe_tool_trace
    assert json.loads(artifact.report_json.read_text())["cases"][0]["passed"] is conformant


@pytest.mark.asyncio
@pytest.mark.parametrize("scorer_name", ("score_candidate", "score_behavior", "_suite_score"))
async def test_scorer_exception_preserves_original_telemetry_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scorer_name: str,
) -> None:
    outcome = _simple_result_with_evidence_value(Decimal("94636.23"))
    model_calls = tuple(
        call.model_copy(
            update={
                "input_tokens": 30,
                "output_tokens": 5,
                "estimated_cost_cny": Decimal("0.01"),
            }
        )
        for call in outcome.safe_trace.model_calls
    )
    nodes = (NodeTrace(node="synthesize", duration_ms=12, outcome="completed"),)
    outcome = outcome.model_copy(
        update={
            "governance": outcome.governance.model_copy(
                update={
                    "input_tokens": 120,
                    "output_tokens": 20,
                    "committed_cost_cny": Decimal("0.04"),
                    "reserved_cost_cny": Decimal("0.02"),
                    "repair_count": 1,
                    "structured_output_repair_count": 1,
                }
            ),
            "safe_trace": outcome.safe_trace.model_copy(
                update={"model_calls": model_calls, "nodes": nodes}
            ),
        }
    )

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("select PRIVATE_ROW from https://private.invalid sk-private")

    monkeypatch.setattr(runner, scorer_name, broken)

    class Executor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            return outcome

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=Executor(),
        cases=(load_week3_cases()[10],),
        output_root=tmp_path,
        run_id="scorer-failure",
    )
    case = artifact.report.cases[0]
    assert not case.passed
    assert not case.suite_conformant
    assert case.observed_final_status is FinalStatus.COMPLETED
    assert case.observed_stop_reason is StopReason.ANSWER_COMPLETE
    assert case.observed_behavior is BehaviorAction.EXECUTE
    assert case.scoring_failure is not None
    assert case.scoring_failure.stage == "scoring"
    assert case.budget_score.input_tokens == 120
    assert case.budget_score.output_tokens == 20
    assert case.budget_score.committed_cost_cny == Decimal("0.04")
    assert case.budget_score.reserved_cost_cny == Decimal("0.02")
    assert case.budget_score.repair_count == 1
    assert case.budget_score.structured_output_repair_count == 1
    assert case.model_trace_calls == 4
    assert len(case.safe_tool_trace) == 3
    assert [t.model_dump() for t in case.safe_model_trace] == [t.model_dump() for t in model_calls]
    assert [t.model_dump() for t in case.safe_node_trace] == [t.model_dump() for t in nodes]
    assert artifact.report.candidate_metrics["final_strict"].denominator == 1
    assert artifact.report.evidence_metric.denominator == 1
    reconstructed = Week3RunReport.model_validate(
        artifact.report.model_dump(exclude_computed_fields=True),
        strict=True,
    )
    assert reconstructed == artifact.report
    stored = json.loads(artifact.report_json.read_text())
    assert stored == artifact.report.model_dump(mode="json")
    assert (
        json.loads((artifact.report_dir / "cases" / "W3K011.json").read_text())
        == stored["cases"][0]
    )
    assert not any(
        token in artifact.report_json.read_text()
        for token in (
            "PRIVATE_ROW",
            "private.invalid",
            "sk-private",
            "94636.23",
        )
    )


@pytest.mark.asyncio
async def test_full_canonical_scoring_failure_report_remains_publishable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("private scorer exception")

    monkeypatch.setattr(runner, "_score_case", broken)

    class Executor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            return _failure(case_id)

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=Executor(),
        output_root=tmp_path,
        run_id="canonical-failures",
    )
    assert artifact.report.report_scope == "canonical"
    assert artifact.report.case_count == 40
    assert artifact.report.passed_count == 0
    assert all(case.scoring_failure is not None for case in artifact.report.cases)
    assert all(not case.suite_score.conformant for case in artifact.report.cases)
    assert len(list((artifact.report_dir / "cases").glob("*.json"))) == 40
    assert "Scoring failures: 40" in artifact.report_markdown.read_text()


@pytest.mark.asyncio
async def test_all_cases_can_publish_lookup_only_early_failures_without_scoring_errors(
    tmp_path: Path,
) -> None:
    class Executor:
        async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
            return _failure(case_id).model_copy(
                update={
                    "governance": GovernanceSnapshot(tool_calls=2),
                    "safe_trace": SafeTrace(
                        tool_calls=tuple(
                            ToolCallTrace(tool_name=tool, purpose=tool.value, safe_arguments=())
                            for tool in (ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP)
                        )
                    ),
                }
            )

    artifact = await run_week3_evaluation(
        mode="fixture",
        executor=Executor(),
        output_root=tmp_path,
        run_id="lookup-only-failures",
    )
    assert artifact.report.case_count == 40
    assert artifact.report.passed_count == 0
    assert all(case.scoring_failure is None for case in artifact.report.cases)
    assert all(case.error_type is None for case in artifact.report.cases)
    assert sum(case.observed_tool_calls for case in artifact.report.cases) == 80
