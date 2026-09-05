from __future__ import annotations

from pathlib import Path

import pytest

from governed_analytics.agent.contracts import StopReason
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
        executor = FixtureWeek3CaseExecutor(
            tools=tools,
            settings=AgentRuntimeSettings.model_validate({}),
        )
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
    # Forty successful/contract-invalid Execute attempts reach the shared backend;
    # W3K030's dangerous statement is rejected by policy before this counter.
    assert len(counting.query_ids) == 40
