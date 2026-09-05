from __future__ import annotations

from decimal import Decimal

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    EvidenceItem,
    Observation,
    ObservationValidation,
    SafeTrace,
    ToolCallTrace,
)
from governed_analytics.evals.models import QueryResult
from governed_analytics.evals.week3.scorers import (
    candidate_observations,
    score_candidate,
    score_evidence,
    score_tool_trace,
)


def _candidate_history() -> AgentRunResult:
    observations = tuple(
        Observation(
            observation_id=f"observation-{index}",
            tool_name=ActionType.EXECUTE_SQL,
            purpose="metric_value_contract",
            ok=True,
            hypothesis_id="metric_value",
            contract_id="metric_value_contract",
            query_id=character * 64,
            columns=("gmv",),
            row_count=1,
            payload={
                "query_id": character * 64,
                "columns": ("gmv",),
                "rows": ((index,),),
                "row_count": 1,
                "row_limit": 500,
                "possibly_truncated": False,
            },
        )
        for index, character in ((1, "a"), (2, "b"))
    )
    validations = (
        ObservationValidation(
            observation_id="observation-1",
            contract_id="metric_value_contract",
            validation_fingerprint="c" * 64,
            valid=False,
            error_code="column_mismatch",
            repairable=True,
        ),
        ObservationValidation(
            observation_id="observation-2",
            contract_id="metric_value_contract",
            validation_fingerprint="d" * 64,
            valid=True,
        ),
    )
    traces = tuple(
        ToolCallTrace(
            tool_name=ActionType.EXECUTE_SQL,
            purpose=observation.purpose,
            safe_arguments=(
                ("contract_id", "metric_value_contract"),
                ("hypothesis_id", "metric_value"),
            ),
            query_id=observation.query_id,
            columns=observation.columns,
            row_count=observation.row_count,
        )
        for observation in observations
    )
    return AgentRunResult.model_construct(
        observations=observations,
        observation_validations=validations,
        safe_trace=SafeTrace(tool_calls=traces),
        first_candidate=observations[0],
    )


def test_candidate_score_keeps_result_contract_and_strict_separate() -> None:
    score = score_candidate(
        comparison="scalar",
        key_columns=(),
        numeric_columns=("gmv",),
        expected=QueryResult(columns=("gmv",), rows=((Decimal("10.00"),),)),
        actual=QueryResult(columns=("value",), rows=((Decimal("10.00"),),)),
        answer_contract_ok=False,
        execution_succeeded=True,
        possibly_truncated=False,
    )

    assert score.result_score == 1
    assert not score.output_contract_conformant
    assert not score.strict_pass


def test_attribution_tool_order_allows_dimension_permutation() -> None:
    calls = tuple(
        ToolCallTrace(tool_name=tool, purpose=purpose, safe_arguments=arguments)
        for tool, purpose, arguments in (
            (ActionType.METRIC_LOOKUP, "metric_lookup", ()),
            (ActionType.SCHEMA_LOOKUP, "schema_lookup", ()),
            (
                ActionType.EXECUTE_SQL,
                "confirm_decline",
                (("contract_id", "gmv_comparison"), ("hypothesis_id", "confirm_decline")),
            ),
            (
                ActionType.EXECUTE_SQL,
                "segment_contribution",
                (
                    ("contract_id", "segment_contribution"),
                    ("hypothesis_id", "segment_contribution"),
                ),
            ),
            (
                ActionType.EXECUTE_SQL,
                "region_contribution",
                (("contract_id", "region_contribution"), ("hypothesis_id", "region_contribution")),
            ),
            (
                ActionType.EXECUTE_SQL,
                "sku_contribution",
                (("contract_id", "sku_contribution"), ("hypothesis_id", "sku_contribution")),
            ),
        )
    )

    score = score_tool_trace(
        required_tools=(ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP, ActionType.EXECUTE_SQL),
        forbidden_tools=(),
        tool_calls=calls,
        required_purposes=(
            "confirm_decline",
            "region_contribution",
            "sku_contribution",
            "segment_contribution",
        ),
    )

    assert score.sequence_conformant
    assert score.conformant


def test_tool_trace_rejects_extra_or_duplicate_execute_safe_triples() -> None:
    prefix = (
        ToolCallTrace(
            tool_name=ActionType.METRIC_LOOKUP, purpose="metric_lookup", safe_arguments=()
        ),
        ToolCallTrace(
            tool_name=ActionType.SCHEMA_LOOKUP, purpose="schema_lookup", safe_arguments=()
        ),
    )
    execute = ToolCallTrace(
        tool_name=ActionType.EXECUTE_SQL,
        purpose="metric_value_contract",
        safe_arguments=(
            ("contract_id", "metric_value_contract"),
            ("hypothesis_id", "metric_value"),
        ),
    )

    score = score_tool_trace(
        required_tools=(ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP, ActionType.EXECUTE_SQL),
        forbidden_tools=(),
        tool_calls=(*prefix, execute, execute),
        required_execute_triples=(
            ("metric_value_contract", "metric_value_contract", "metric_value"),
        ),
    )

    assert not score.sequence_conformant
    assert not score.conformant


def test_candidate_requires_earliest_first_unique_fingerprint_and_trace_bijection() -> None:
    result = _candidate_history()
    first, final, first_valid, final_valid = candidate_observations(
        result, purpose="metric_value_contract"
    )
    assert first == result.observations[0]
    assert final == result.observations[1]
    assert not first_valid
    assert final_valid

    replayed_first = result.model_copy(update={"first_candidate": result.observations[1]})
    assert candidate_observations(replayed_first, purpose="metric_value_contract")[0] is None

    duplicate_fingerprint = result.model_copy(
        update={
            "observation_validations": (
                result.observation_validations[0],
                result.observation_validations[1].model_copy(
                    update={"validation_fingerprint": "c" * 64}
                ),
            )
        }
    )
    assert candidate_observations(duplicate_fingerprint, purpose="metric_value_contract")[1] is None

    orphan_validation = result.model_copy(
        update={
            "observation_validations": (
                *result.observation_validations,
                result.observation_validations[-1].model_copy(
                    update={
                        "observation_id": "orphan",
                        "validation_fingerprint": "e" * 64,
                    }
                ),
            )
        }
    )
    assert candidate_observations(orphan_validation, purpose="metric_value_contract")[1] is None

    duplicate_trace = result.model_copy(
        update={
            "safe_trace": SafeTrace(
                tool_calls=(*result.safe_trace.tool_calls, result.safe_trace.tool_calls[-1])
            )
        }
    )
    assert candidate_observations(duplicate_trace, purpose="metric_value_contract")[1] is None


def test_evidence_rejects_orphan_duplicate_and_trace_metadata_mismatch() -> None:
    result = _candidate_history()
    evidence = EvidenceItem(
        evidence_id="evidence-1",
        observation_id="observation-2",
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        query_id="b" * 64,
        claim_key="metric_value",
        stance="supports",
        verified=True,
    )
    score = score_evidence(
        required_purposes=("metric_value_contract",),
        evidence=(evidence,),
        observations=result.observations,
        validations=result.observation_validations,
        tool_calls=result.safe_trace.tool_calls,
    )
    assert score.verified_count == 1

    orphan = evidence.model_copy(update={"observation_id": "orphan"})
    assert (
        score_evidence(
            required_purposes=("metric_value_contract",),
            evidence=(orphan,),
            observations=result.observations,
            validations=result.observation_validations,
            tool_calls=result.safe_trace.tool_calls,
        ).verified_count
        == 0
    )
    assert (
        score_evidence(
            required_purposes=("metric_value_contract",),
            evidence=(evidence, evidence),
            observations=result.observations,
            validations=result.observation_validations,
            tool_calls=result.safe_trace.tool_calls,
        ).verified_count
        == 0
    )
    assert (
        score_evidence(
            required_purposes=("metric_value_contract",),
            evidence=(evidence,),
            observations=(),
            validations=(),
            tool_calls=(),
        ).verified_count
        == 0
    )

    mismatched_trace = result.safe_trace.tool_calls[-1].model_copy(update={"columns": ("net_gmv",)})
    assert (
        score_evidence(
            required_purposes=("metric_value_contract",),
            evidence=(evidence,),
            observations=result.observations,
            validations=result.observation_validations,
            tool_calls=(*result.safe_trace.tool_calls[:-1], mismatched_trace),
        ).verified_count
        == 0
    )
