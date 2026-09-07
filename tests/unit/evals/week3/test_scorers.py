from __future__ import annotations

from decimal import Decimal

import pytest

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    AnswerContract,
    ColumnContract,
    EvidenceItem,
    Observation,
    ObservationContract,
    ObservationValidation,
    ResultShape,
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


def _simple_answer_contract() -> AnswerContract:
    return AnswerContract(
        answer_contract_id="simple-answer",
        required_hypotheses=("metric_value",),
        observation_contracts=(
            ObservationContract(
                contract_id="metric_value_contract",
                hypothesis_id="metric_value",
                columns=(
                    ColumnContract(
                        name="gmv",
                        data_type="decimal",
                        role="metric",
                        unit="cny",
                    ),
                ),
                shape=ResultShape.SCALAR,
                min_rows=1,
                max_rows=1,
            ),
        ),
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
        claim_key="gmv",
        stance="supports",
        numeric_value=Decimal("2"),
        unit="cny",
        verified=True,
    )
    score = score_evidence(
        required_purposes=("metric_value_contract",),
        answer_contract=_simple_answer_contract(),
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
            answer_contract=_simple_answer_contract(),
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
            answer_contract=_simple_answer_contract(),
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
            answer_contract=_simple_answer_contract(),
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
            answer_contract=_simple_answer_contract(),
            evidence=(evidence,),
            observations=result.observations,
            validations=result.observation_validations,
            tool_calls=(*result.safe_trace.tool_calls[:-1], mismatched_trace),
        ).verified_count
        == 0
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"claim_key": "net_gmv"},
        {"dimensions": (("region", "north"),)},
        {"stance": "refutes"},
        {"numeric_value": Decimal("999999")},
        {"unit": "usd"},
    ),
)
def test_evidence_claim_projection_must_match_the_linked_result(
    changes: dict[str, object],
) -> None:
    result = _candidate_history()
    evidence = EvidenceItem(
        evidence_id="evidence-1",
        observation_id="observation-2",
        hypothesis_id="metric_value",
        contract_id="metric_value_contract",
        query_id="b" * 64,
        claim_key="gmv",
        stance="supports",
        numeric_value=Decimal("2"),
        unit="cny",
        verified=True,
    ).model_copy(update=changes)

    score = score_evidence(
        required_purposes=("metric_value_contract",),
        answer_contract=_simple_answer_contract(),
        evidence=(evidence,),
        observations=result.observations,
        validations=result.observation_validations,
        tool_calls=result.safe_trace.tool_calls,
    )

    assert score.verified_count == 0
    assert not score.oracle_verified_sufficient


def test_evidence_requires_the_complete_attribution_claim_multiset() -> None:
    query_id = "e" * 64
    observation = Observation(
        observation_id="observation-region",
        tool_name=ActionType.EXECUTE_SQL,
        purpose="region_contribution",
        ok=True,
        hypothesis_id="region_contribution",
        contract_id="region_contribution",
        query_id=query_id,
        columns=("region", "gmv_loss"),
        row_count=2,
        payload={
            "query_id": query_id,
            "columns": ("region", "gmv_loss"),
            "rows": (("north", "10.00"), ("south", "20.00")),
            "row_count": 2,
            "row_limit": 500,
            "possibly_truncated": False,
        },
    )
    validation = ObservationValidation(
        observation_id=observation.observation_id,
        contract_id="region_contribution",
        validation_fingerprint="f" * 64,
        valid=True,
    )
    trace = ToolCallTrace(
        tool_name=ActionType.EXECUTE_SQL,
        purpose="region_contribution",
        safe_arguments=(
            ("contract_id", "region_contribution"),
            ("hypothesis_id", "region_contribution"),
        ),
        query_id=query_id,
        columns=observation.columns,
        row_count=observation.row_count,
    )
    answer_contract = AnswerContract(
        answer_contract_id="attribution-answer",
        required_hypotheses=("region_contribution",),
        observation_contracts=(
            ObservationContract(
                contract_id="region_contribution",
                hypothesis_id="region_contribution",
                columns=(
                    ColumnContract(name="region", data_type="string", role="dimension"),
                    ColumnContract(
                        name="gmv_loss",
                        data_type="decimal",
                        role="metric",
                        unit="cny",
                    ),
                ),
                shape=ResultShape.TABLE,
                min_rows=1,
                max_rows=10,
                key_columns=("region",),
            ),
        ),
    )
    claims = tuple(
        EvidenceItem(
            evidence_id=f"evidence-{region}",
            observation_id=observation.observation_id,
            hypothesis_id="region_contribution",
            contract_id="region_contribution",
            query_id=query_id,
            claim_key="gmv_loss",
            dimensions=(("region", region),),
            stance="supports",
            numeric_value=Decimal(value),
            unit="cny",
            verified=True,
        )
        for region, value in (("north", "10.00"), ("south", "20.00"))
    )

    def is_sufficient(items: tuple[EvidenceItem, ...]) -> bool:
        return score_evidence(
            required_purposes=("region_contribution",),
            answer_contract=answer_contract,
            evidence=items,
            observations=(observation,),
            validations=(validation,),
            tool_calls=(trace,),
        ).oracle_verified_sufficient

    assert is_sufficient(claims)
    assert not is_sufficient(claims[:1])
    assert not is_sufficient((*claims, claims[0]))


@pytest.mark.parametrize("lookup_count", (0, 1, 2))
def test_tool_sequence_without_execute_is_nonconformant_not_an_exception(lookup_count: int) -> None:
    tools = (ActionType.METRIC_LOOKUP, ActionType.SCHEMA_LOOKUP)
    trace = tuple(
        ToolCallTrace(tool_name=name, purpose=name.value, safe_arguments=())
        for name in tools[:lookup_count]
    )
    score = score_tool_trace(
        required_tools=(*tools, ActionType.EXECUTE_SQL),
        forbidden_tools=(),
        tool_calls=trace,
        required_execute_triples=(
            ("metric_value_contract", "metric_value_contract", "metric_value"),
        ),
    )
    assert not score.required_present
    assert not score.sequence_conformant
    assert not score.conformant
