from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from governed_analytics.agent.contracts import AgentRunResult, StopReason
from governed_analytics.agent.tool_registry import ToolRegistry
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings
from governed_analytics.evals.week3.runner import (
    FixtureWeek3CaseExecutor,
    run_week3_evaluation,
)
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.safety.sql_policy import ValidatedSql
from governed_analytics.tools import (
    AsyncEngineSqlExecutionBackend,
    ExecuteSqlTool,
    MetricTool,
    ProfileTool,
    SchemaTool,
)
from governed_analytics.tools.contracts import QueryResult


class _CountingBackend:
    def __init__(self, backend: AsyncEngineSqlExecutionBackend) -> None:
        self.backend = backend
        self.query_ids: list[str] = []

    async def execute(self, validated: ValidatedSql, parameters: tuple[object, ...]) -> QueryResult:
        self.query_ids.append(validated.query_id)
        return await self.backend.execute(validated, parameters)


class _AttributingExecutor:
    def __init__(self, executor: FixtureWeek3CaseExecutor, backend: _CountingBackend) -> None:
        self.executor = executor
        self.backend = backend
        self.settings = executor.settings
        self.calls_by_case: dict[str, int] = {}

    def bind_execution_catalog(self, catalog: Any) -> None:
        self.executor.bind_execution_catalog(catalog)

    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        before = len(self.backend.query_ids)
        result = await self.executor.run_case(case_id=case_id, question=question)
        self.calls_by_case[case_id] = len(self.backend.query_ids) - before
        return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fixture_runner_executes_all_40_cases_on_one_shared_readonly_engine(
    tmp_path: Path,
) -> None:
    engine = create_async_database_engine(DatabaseSettings())  # type: ignore[call-arg]
    counting = _CountingBackend(AsyncEngineSqlExecutionBackend(engine))
    try:
        tools = ToolRegistry.default(
            SchemaTool(),
            MetricTool(),
            ProfileTool(backend=counting),
            ExecuteSqlTool(backend=counting),
        )
        fixture_executor = FixtureWeek3CaseExecutor(
            tools=tools,
            settings=AgentRuntimeSettings.model_validate({}),
        )
        executor = _AttributingExecutor(fixture_executor, counting)
        artifact = await run_week3_evaluation(
            mode="fixture",
            executor=executor,
            output_root=tmp_path,
            run_id="week3-integration",
        )
    finally:
        await engine.dispose()

    by_id = {case.case_id: case for case in artifact.report.cases}
    assert artifact.report.case_count == artifact.report.passed_count == 40
    assert artifact.report.report_scope == "canonical"
    assert artifact.report.behavior_metric.denominator == 10
    assert artifact.report.simple_metric.denominator == 15
    assert artifact.report.attribution_metric.denominator == 1
    assert artifact.report.heldout_metric.denominator == 10
    assert artifact.report.repair_metric.denominator == 2
    assert artifact.report.candidate_metrics["first_strict"].denominator < 40
    assert artifact.report.candidate_metrics["final_strict"].denominator < 40
    assert (
        sum(
            case.cohort == "known" and case.suite == "behavior" and case.behavior_conformant
            for case in artifact.report.cases
        )
        == 10
    )
    assert (
        sum(
            case.suite == "simple"
            and case.cohort == "known"
            and case.final_candidate_score is not None
            and case.final_candidate_score.strict_pass
            for case in artifact.report.cases
        )
        == 15
    )
    assert by_id["W3K026"].evidence_score is not None
    assert by_id["W3K026"].evidence_score.verified_count == 4
    assert by_id["W3K027"].observed_repair_count == 1
    assert by_id["W3K027"].final_conformant
    assert by_id["W3K028"].observed_repair_count == 1
    assert by_id["W3K028"].observed_stop_reason is StopReason.REPAIR_FAILED
    assert by_id["W3K028"].final_candidate_score is None
    assert by_id["W3K029"].observed_tool_calls == 3
    assert by_id["W3K029"].observed_stop_reason is StopReason.EVIDENCE_PARTIAL
    assert by_id["W3K030"].observed_repair_count == 0
    assert by_id["W3K030"].observed_stop_reason is StopReason.SQL_POLICY_REJECTED

    def triples(case_id: str) -> tuple[tuple[str, str | None, str | None], ...]:
        return tuple(
            (trace.purpose, trace.contract_id, trace.hypothesis_id)
            for trace in by_id[case_id].safe_tool_trace
            if trace.tool_name.value == "execute_sql"
        )

    simple = ("metric_value_contract", "metric_value_contract", "metric_value")
    assert triples("W3K027") == (simple, simple)
    assert triples("W3K028") == (simple, simple)
    assert triples("W3K029") == (("gmv_comparison", "gmv_comparison", "confirm_decline"),)
    assert triples("W3K030") == (simple,)
    assert tuple(
        trace.safe_error
        for trace in by_id["W3K030"].safe_tool_trace
        if trace.tool_name.value == "execute_sql"
    ) == ("read_only_policy",)
    assert by_id["W3K029"].evidence_score is not None
    assert by_id["W3K029"].evidence_score.verified_count == 1
    assert len(by_id["W3K029"].evidence_references) == 1
    assert by_id["W3K030"].valid_execute_count == 0
    assert not by_id["W3K030"].evidence_references
    assert executor.calls_by_case["W3K030"] == 0
    for case_id, case_result in by_id.items():
        completed_execute_count = sum(
            trace.tool_name.value == "execute_sql" and trace.outcome == "completed"
            for trace in case_result.safe_tool_trace
        )
        assert len(case_result.safe_validation_refs) == completed_execute_count
        assert all(
            validation.result_sha256 is not None
            for validation in case_result.safe_validation_refs
            if validation.valid
        )
        assert executor.calls_by_case[case_id] == sum(
            trace.tool_name.value == "execute_sql" and trace.query_id is not None
            for trace in case_result.safe_tool_trace
        )
    # Forty successful/contract-invalid Execute attempts reach the shared backend;
    # W3K030's dangerous statement is rejected by policy before this counter.
    assert len(counting.query_ids) == 40
