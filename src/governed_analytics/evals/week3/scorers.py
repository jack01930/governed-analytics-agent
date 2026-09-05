"""Independent, public-result-only scoring for the Week 3 protocol."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    BehaviorAction,
    EvidenceItem,
    Observation,
    ObservationValidation,
    ToolCallTrace,
)
from governed_analytics.evals.models import QueryResult as EvalQueryResult
from governed_analytics.evals.week3.models import (
    BehaviorScore,
    CandidateScore,
    EvidenceScore,
    ToolScore,
)
from governed_analytics.tools.contracts import QueryResult as ProductionQueryResult

_ZERO = Decimal("0")
_ONE = Decimal("1")
type ExecuteTriple = tuple[str, str, str]


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation:
            return None
    else:
        return None
    return candidate if candidate.is_finite() else None


def _numeric_equal(expected: object, actual: object, absolute: Decimal, relative: Decimal) -> bool:
    expected_number = _decimal(expected)
    actual_number = _decimal(actual)
    if expected_number is None or actual_number is None:
        return False
    difference = abs(expected_number - actual_number)
    return difference <= absolute or difference <= abs(expected_number) * relative


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    return None


def _rows_match(
    expected: Sequence[object],
    actual: Sequence[object],
    numeric_indexes: frozenset[int],
    absolute: Decimal,
    relative: Decimal,
) -> bool:
    return len(expected) == len(actual) and all(
        _numeric_equal(expected_value, actual[index], absolute, relative)
        if index in numeric_indexes
        else expected_value == actual[index]
        for index, expected_value in enumerate(expected)
    )


def _result_score(
    *,
    comparison: str,
    key_columns: tuple[str, ...],
    numeric_columns: tuple[str, ...],
    expected: EvalQueryResult,
    actual: EvalQueryResult,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
) -> Decimal:
    try:
        if len(expected.columns) != len(actual.columns):
            return _ZERO
        numeric_indexes = frozenset(expected.columns.index(name) for name in numeric_columns)
        key_indexes = tuple(expected.columns.index(name) for name in key_columns)
        if comparison == "scalar":
            if len(expected.columns) != 1 or len(expected.rows) != 1 or len(actual.rows) != 1:
                return _ZERO
            return (
                _ONE
                if _numeric_equal(
                    expected.rows[0][0], actual.rows[0][0], absolute_tolerance, relative_tolerance
                )
                else _ZERO
            )
        if comparison == "boolean":
            if len(expected.columns) != 1 or len(expected.rows) != 1 or len(actual.rows) != 1:
                return _ZERO
            expected_value, actual_value = (
                _boolean(expected.rows[0][0]),
                _boolean(actual.rows[0][0]),
            )
            return _ONE if expected_value is not None and expected_value == actual_value else _ZERO
        if comparison not in {"table", "top_k"}:
            return _ZERO
        if len(expected.rows) != len(actual.rows):
            return _ZERO
        if comparison == "top_k":
            pairs = tuple(zip(expected.rows, actual.rows, strict=True))
        elif key_indexes:
            expected_map = {
                tuple(row[index] for index in key_indexes): row for row in expected.rows
            }
            actual_map = {tuple(row[index] for index in key_indexes): row for row in actual.rows}
            if (
                len(expected_map) != len(expected.rows)
                or len(actual_map) != len(actual.rows)
                or set(expected_map) != set(actual_map)
            ):
                return _ZERO
            pairs = tuple((row, actual_map[key]) for key, row in expected_map.items())
        else:
            pairs = tuple(zip(expected.rows, actual.rows, strict=True))
        if comparison == "top_k" and any(
            tuple(expected_row[index] for index in key_indexes)
            != tuple(actual_row[index] for index in key_indexes)
            for expected_row, actual_row in pairs
        ):
            return _ZERO
        return (
            _ONE
            if all(
                _rows_match(
                    expected_row,
                    actual_row,
                    numeric_indexes,
                    absolute_tolerance,
                    relative_tolerance,
                )
                for expected_row, actual_row in pairs
            )
            else _ZERO
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return _ZERO


def score_candidate(
    *,
    comparison: str,
    key_columns: tuple[str, ...],
    numeric_columns: tuple[str, ...],
    expected: EvalQueryResult,
    actual: EvalQueryResult | None,
    answer_contract_ok: bool,
    execution_succeeded: bool,
    possibly_truncated: bool,
    absolute_tolerance: Decimal = Decimal("0.01"),
    relative_tolerance: Decimal = Decimal("0.000001"),
) -> CandidateScore:
    """Keep value, alias, production validation, execution and truncation independent."""
    result_score = (
        _ZERO
        if actual is None
        else _result_score(
            comparison=comparison,
            key_columns=key_columns,
            numeric_columns=numeric_columns,
            expected=expected,
            actual=actual,
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
        )
    )
    output_contract = actual is not None and actual.columns == expected.columns
    strict = (
        result_score == _ONE
        and output_contract
        and answer_contract_ok
        and execution_succeeded
        and not possibly_truncated
    )
    return CandidateScore(
        result_score=result_score,
        output_contract_conformant=output_contract,
        answer_contract_validated=answer_contract_ok,
        execution_succeeded=execution_succeeded,
        possibly_truncated=possibly_truncated,
        strict_pass=strict,
    )


def restore_query_result(observation: Observation) -> ProductionQueryResult | None:
    """Strictly restore the production payload and bind every outer metadata field."""
    if observation.tool_name is not ActionType.EXECUTE_SQL or not observation.ok:
        return None
    payload = observation.payload
    if not isinstance(payload, Mapping):
        return None
    try:
        restored = ProductionQueryResult.model_validate(dict(payload), strict=True)
    except (TypeError, ValueError, ValidationError):
        return None
    if (
        restored.query_id != observation.query_id
        or restored.columns != observation.columns
        or restored.row_count != observation.row_count
        or restored.possibly_truncated != observation.possibly_truncated
    ):
        return None
    return restored


def candidate_observations(
    result: AgentRunResult, *, purpose: str
) -> tuple[Observation | None, Observation | None, bool, bool]:
    """Return exact first and last valid candidates without reading private graph state."""
    expected_contract_id = "gmv_comparison" if purpose == "confirm_decline" else purpose
    matching = tuple(
        item
        for item in result.observations
        if item.tool_name is ActionType.EXECUTE_SQL
        and (item.purpose == purpose or item.hypothesis_id == purpose)
        and item.contract_id == expected_contract_id
    )
    first = result.first_candidate
    first_history_ok = (
        first is not None
        and sum(item == first for item in result.observations) == 1
        and bool(matching)
        and first == matching[0]
    )
    if not first_history_ok:
        first = None
    counts = Counter(item.observation_id for item in result.observation_validations)
    validations = {
        item.observation_id: item
        for item in result.observation_validations
        if counts[item.observation_id] == 1
    }
    fingerprints_unique = len(
        {item.validation_fingerprint for item in result.observation_validations}
    ) == len(result.observation_validations)
    linkage_ok = execute_linkage_is_bijective(
        result.observations, result.observation_validations, result.safe_trace.tool_calls
    )
    valid: list[Observation] = []
    for observation in matching:
        validation = validations.get(observation.observation_id)
        trace_matches = tuple(
            trace
            for trace in result.safe_trace.tool_calls
            if trace.tool_name is ActionType.EXECUTE_SQL
            and trace.query_id == observation.query_id
            and trace.purpose == observation.purpose
            and dict(trace.safe_arguments).get("contract_id") == observation.contract_id
            and dict(trace.safe_arguments).get("hypothesis_id") == observation.hypothesis_id
        )
        if (
            validation is not None
            and fingerprints_unique
            and linkage_ok
            and validation.valid
            and validation.contract_id == observation.contract_id
            and len(validation.validation_fingerprint) == 64
            and len(trace_matches) == 1
            and restore_query_result(observation) is not None
        ):
            valid.append(observation)
    final = valid[-1] if valid else None
    first_valid = first is not None and any(item == first for item in valid)
    return first, final, first_valid, final is not None


def score_behavior(
    *,
    expected_action: str,
    expected_missing_fields: tuple[str, ...],
    behavior_action: BehaviorAction | None,
    behavior_reason: str | None,
    observed_missing_fields: tuple[str, ...],
    expected_stop_reason: str | None = None,
) -> BehaviorScore:
    action_ok = behavior_action is not None and behavior_action.value == expected_action
    expected_reason_by_stop = {
        "missing_required_fields": {
            "missing_metric",
            "missing_time_window",
            "missing_comparison_window",
            "ambiguous_metric",
        },
        "unsafe_request": {"unsafe_request"},
        "sensitive_data_request": {"sensitive_data_request"},
        "unsupported_analysis": {"unsupported_analysis"},
        "unsupported_data_domain": {"unsupported_data_domain"},
        "answer_complete": {"ready"},
        "evidence_partial": {"ready"},
        "sql_policy_rejected": {"ready"},
        "answer_contract_unmet": {"ready"},
    }
    allowed_reasons = expected_reason_by_stop.get(expected_stop_reason or "", {"ready"})
    reason_ok = behavior_reason in allowed_reasons
    missing_ok = observed_missing_fields == expected_missing_fields
    return BehaviorScore(
        action_conformant=action_ok,
        reason_conformant=reason_ok,
        missing_fields_conformant=missing_ok,
        conformant=action_ok and reason_ok and missing_ok,
    )


def score_tool_trace(
    *,
    required_tools: tuple[ActionType, ...],
    forbidden_tools: tuple[ActionType, ...],
    tool_calls: tuple[ToolCallTrace, ...],
    required_purposes: tuple[str, ...] = (),
    required_execute_triples: tuple[ExecuteTriple, ...] = (),
) -> ToolScore:
    names = tuple(item.tool_name for item in tool_calls)
    required_present = set(required_tools).issubset(names)
    forbidden_absent = not set(forbidden_tools).intersection(names)
    positions = {name: names.index(name) for name in set(names)}
    sequence_ok = True
    if ActionType.EXECUTE_SQL in required_tools:
        sequence_ok = (
            ActionType.METRIC_LOOKUP in positions
            and ActionType.SCHEMA_LOOKUP in positions
            and positions[ActionType.METRIC_LOOKUP] < positions[ActionType.SCHEMA_LOOKUP]
            and positions[ActionType.SCHEMA_LOOKUP] < positions[ActionType.EXECUTE_SQL]
        )
    if required_execute_triples:
        actual_triples = tuple(
            (
                item.purpose,
                str(dict(item.safe_arguments).get("contract_id", "")),
                str(dict(item.safe_arguments).get("hypothesis_id", "")),
            )
            for item in tool_calls
            if item.tool_name is ActionType.EXECUTE_SQL
        )
        sequence_ok = sequence_ok and Counter(actual_triples) == Counter(required_execute_triples)
        ordered_purposes = tuple(item[2] for item in actual_triples)
        if "confirm_decline" in ordered_purposes:
            confirm_index = ordered_purposes.index("confirm_decline")
            sequence_ok = sequence_ok and all(
                confirm_index < ordered_purposes.index(purpose)
                for purpose in {"region_contribution", "sku_contribution", "segment_contribution"}
                if purpose in ordered_purposes
            )
    elif required_purposes:
        execute_purposes = tuple(
            item.purpose
            if item.purpose in required_purposes
            else str(dict(item.safe_arguments).get("hypothesis_id", item.purpose))
            for item in tool_calls
            if item.tool_name is ActionType.EXECUTE_SQL
        )
        counts = Counter(execute_purposes)
        sequence_ok = sequence_ok and all(counts[purpose] == 1 for purpose in required_purposes)
        if "confirm_decline" in required_purposes and "confirm_decline" in execute_purposes:
            confirm_index = execute_purposes.index("confirm_decline")
            sequence_ok = sequence_ok and all(
                confirm_index < execute_purposes.index(purpose)
                for purpose in required_purposes
                if purpose != "confirm_decline" and purpose in execute_purposes
            )
    return ToolScore(
        required_present=required_present,
        forbidden_absent=forbidden_absent,
        sequence_conformant=sequence_ok,
        conformant=required_present and forbidden_absent and sequence_ok,
    )


def _observation_trace_key(observation: Observation) -> tuple[object, ...]:
    return (
        observation.query_id,
        observation.purpose,
        observation.contract_id,
        observation.hypothesis_id,
        observation.columns,
        observation.row_count,
        observation.possibly_truncated,
    )


def _tool_trace_key(trace: ToolCallTrace) -> tuple[object, ...]:
    arguments = dict(trace.safe_arguments)
    return (
        trace.query_id,
        trace.purpose,
        arguments.get("contract_id"),
        arguments.get("hypothesis_id"),
        trace.columns,
        trace.row_count,
        trace.possibly_truncated,
    )


def execute_linkage_is_bijective(
    observations: tuple[Observation, ...],
    validations: tuple[ObservationValidation, ...],
    tool_calls: tuple[ToolCallTrace, ...],
) -> bool:
    successful = tuple(
        item for item in observations if item.tool_name is ActionType.EXECUTE_SQL and item.ok
    )
    validation_counts = Counter(item.observation_id for item in validations)
    if validation_counts != Counter(item.observation_id for item in successful):
        return False
    by_id = {item.observation_id: item for item in successful}
    if any(by_id[item.observation_id].contract_id != item.contract_id for item in validations):
        return False
    if len({item.validation_fingerprint for item in validations}) != len(validations):
        return False
    completed_traces = tuple(
        item
        for item in tool_calls
        if item.tool_name is ActionType.EXECUTE_SQL and item.safe_error is None
    )
    return Counter(map(_observation_trace_key, successful)) == Counter(
        map(_tool_trace_key, completed_traces)
    )


def score_evidence(
    *,
    required_purposes: tuple[str, ...],
    evidence: tuple[EvidenceItem, ...],
    observations: tuple[Observation, ...] = (),
    validations: tuple[ObservationValidation, ...] = (),
    tool_calls: tuple[ToolCallTrace, ...] = (),
) -> EvidenceScore:
    observation_counts = Counter(item.observation_id for item in observations)
    observation_by_id = {
        item.observation_id: item
        for item in observations
        if observation_counts[item.observation_id] == 1
    }
    validation_counts = Counter(item.observation_id for item in validations)
    validation_by_id = {
        item.observation_id: item
        for item in validations
        if validation_counts[item.observation_id] == 1
    }
    evidence_ids_unique = len({item.evidence_id for item in evidence}) == len(evidence)
    links_ok = bool(observations) and (
        len({item.validation_fingerprint for item in validations}) == len(validations)
        and (not tool_calls or execute_linkage_is_bijective(observations, validations, tool_calls))
    )
    verified_purposes: list[str] = []
    for item in evidence:
        if not item.verified or not evidence_ids_unique:
            continue
        observation = observation_by_id.get(item.observation_id)
        validation = validation_by_id.get(item.observation_id)
        if (
            not links_ok
            or observation is None
            or validation is None
            or not validation.valid
            or observation.contract_id != item.contract_id
            or observation.hypothesis_id != item.hypothesis_id
            or observation.query_id != item.query_id
            or (
                observation.purpose not in required_purposes
                and observation.hypothesis_id not in required_purposes
            )
            or validation.contract_id != item.contract_id
            or restore_query_result(observation) is None
        ):
            continue
        purpose = (
            observation.purpose
            if observation.purpose in required_purposes
            else observation.hypothesis_id or observation.purpose
        )
        verified_purposes.append(purpose)
    counts = Counter(verified_purposes)
    verified_count = sum(counts[purpose] >= 1 for purpose in required_purposes)
    required_count = len(required_purposes)
    return EvidenceScore(
        required_count=required_count,
        verified_count=verified_count,
        oracle_verified_sufficient=verified_count == required_count,
    )


__all__ = [
    "ExecuteTriple",
    "candidate_observations",
    "execute_linkage_is_bijective",
    "restore_query_result",
    "score_behavior",
    "score_candidate",
    "score_evidence",
    "score_tool_trace",
]
