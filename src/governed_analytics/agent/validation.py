"""Pure contract validation, evidence extraction, and repair classification."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import pairwise
from typing import Literal, cast

from pydantic import ValidationError

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    ColumnContract,
    EvidenceAssessment,
    EvidenceItem,
    GovernanceSnapshot,
    Observation,
    ObservationContract,
    ObservationValidation,
    RepairDecision,
    RepairRecord,
    SortKey,
    StopReason,
    TypedMetricPlan,
)
from governed_analytics.tools.contracts import QueryResult

_REPAIRABLE_ERRORS = frozenset(
    {
        "column_contract_mismatch",
        "key_not_unique",
        "limit_exceeded",
        "nullable_contract_mismatch",
        "order_contract_mismatch",
        "row_count_mismatch",
        "row_shape_mismatch",
        "type_contract_mismatch",
        "zero_denominator_contract_mismatch",
    }
)
_FAILED_TOOL_ERRORS: Mapping[str, str] = {
    "sql_timeout": "sql_timeout",
    "read_only_policy": "policy_blocked",
    "forbidden_relation": "policy_blocked",
    "forbidden_function": "policy_blocked",
    "nondeterministic_query": "policy_blocked",
    "output_shape_policy": "policy_blocked",
    "sensitive_output": "sensitive_result_blocked",
    "database_error": "database_error",
}
_ATTRIBUTION_DIMENSIONS = frozenset(
    {"region_contribution", "sku_contribution", "segment_contribution"}
)


def _validation_fingerprint(
    action: AgentAction,
    observation: Observation,
    contract: ObservationContract,
) -> str:
    canonical = json.dumps(
        {
            "action": action.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
            "contract": contract.model_dump(mode="json"),
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(bytes(canonical, "utf-8")).hexdigest()


def _invalid(
    observation: Observation,
    contract: ObservationContract,
    validation_fingerprint: str,
    error_code: str,
    *,
    repairable: bool = False,
) -> ObservationValidation:
    return ObservationValidation(
        observation_id=observation.observation_id,
        contract_id=contract.contract_id,
        validation_fingerprint=validation_fingerprint,
        valid=False,
        error_code=error_code,
        repairable=repairable and error_code in _REPAIRABLE_ERRORS,
    )


def _restore_query_result(
    observation: Observation,
    contract: ObservationContract,
    validation_fingerprint: str,
) -> QueryResult | ObservationValidation:
    payload = observation.payload
    if not isinstance(payload, Mapping):
        return _invalid(
            observation, contract, validation_fingerprint, "invalid_query_result_payload"
        )
    rows = payload.get("rows")
    columns = payload.get("columns")
    if not isinstance(rows, tuple) or not isinstance(columns, tuple):
        return _invalid(
            observation, contract, validation_fingerprint, "invalid_query_result_payload"
        )
    if any(not isinstance(row, tuple) or len(row) != len(columns) for row in rows):
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            "row_shape_mismatch",
            repairable=True,
        )
    if payload.get("row_count") != len(rows):
        return _invalid(
            observation, contract, validation_fingerprint, "query_result_mismatch"
        )
    try:
        result = QueryResult.model_validate(dict(payload))
    except (TypeError, ValueError, ValidationError):
        return _invalid(
            observation, contract, validation_fingerprint, "invalid_query_result_payload"
        )
    if (
        result.query_id != observation.query_id
        or result.columns != observation.columns
        or result.row_count != observation.row_count
        or result.possibly_truncated != observation.possibly_truncated
    ):
        return _invalid(
            observation, contract, validation_fingerprint, "query_result_mismatch"
        )
    return result


def _decimal(value: object) -> Decimal | None:
    if type(value) not in {str, int, float, Decimal}:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _valid_type(value: object, column: ColumnContract) -> bool:
    if value is None:
        return column.nullable
    if column.data_type == "string":
        return type(value) is str
    if column.data_type == "integer":
        return type(value) is int
    if column.data_type == "decimal":
        return _decimal(value) is not None
    if column.data_type == "boolean":
        return type(value) is bool
    if column.data_type == "date":
        if type(value) is not str:
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        return parsed.isoformat() == value
    if column.data_type == "datetime":
        if type(value) is not str:
            return False
        try:
            parsed_datetime = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed_datetime.tzinfo is not None and parsed_datetime.utcoffset() is not None
    return False


def _compare(
    left: object,
    right: object,
    sort: SortKey,
    column: ColumnContract,
) -> int:
    if left is None or right is None:
        if left is right:
            return 0
        left_first = sort.nulls == "first"
        return -1 if (left is None) == left_first else 1
    if column.data_type == "decimal":
        decimal_left = _decimal(left)
        decimal_right = _decimal(right)
        if decimal_left is None or decimal_right is None:
            raise ValueError("validated decimal sort value is missing")
        comparison = (decimal_left > decimal_right) - (decimal_left < decimal_right)
    elif column.data_type == "integer":
        integer_left = cast(int, left)
        integer_right = cast(int, right)
        comparison = (integer_left > integer_right) - (integer_left < integer_right)
    elif column.data_type == "boolean":
        boolean_left = cast(bool, left)
        boolean_right = cast(bool, right)
        comparison = (boolean_left > boolean_right) - (boolean_left < boolean_right)
    elif column.data_type == "date":
        date_left = date.fromisoformat(cast(str, left))
        date_right = date.fromisoformat(cast(str, right))
        comparison = (date_left > date_right) - (date_left < date_right)
    elif column.data_type == "datetime":
        datetime_left = datetime.fromisoformat(cast(str, left))
        datetime_right = datetime.fromisoformat(cast(str, right))
        comparison = (datetime_left > datetime_right) - (
            datetime_left < datetime_right
        )
    else:
        string_left = cast(str, left)
        string_right = cast(str, right)
        comparison = (string_left > string_right) - (string_left < string_right)
    return comparison if sort.direction == "asc" else -comparison


def _ordered(
    rows: tuple[tuple[object, ...], ...],
    contract: ObservationContract,
) -> bool:
    indexes = {name: index for index, name in enumerate(contract.column_names)}
    columns = {column.name: column for column in contract.columns}
    for left, right in pairwise(rows):
        for sort in contract.order_by:
            comparison = _compare(
                left[indexes[sort.column]],
                right[indexes[sort.column]],
                sort,
                columns[sort.column],
            )
            if comparison < 0:
                break
            if comparison > 0:
                return False
    return True


def _ratio_policy_error(
    contract: ObservationContract,
    result: QueryResult,
) -> str | None:
    if contract.contract_id != "gmv_comparison" or result.row_count != 1:
        return None
    indexes = {name: index for index, name in enumerate(result.columns)}
    previous = _decimal(result.rows[0][indexes["previous_gmv"]])
    ratio = result.rows[0][indexes["change_rate"]]
    if previous == 0:
        return None if ratio is None else "zero_denominator_contract_mismatch"
    return "nullable_contract_mismatch" if ratio is None else None


def validate_observation(
    action: AgentAction,
    observation: Observation,
    contract: ObservationContract,
) -> ObservationValidation:
    """Validate one Execute Observation against exactly one referenced contract."""

    validation_fingerprint = _validation_fingerprint(action, observation, contract)
    if (
        action.action_type is not ActionType.EXECUTE_SQL
        or observation.tool_name is not ActionType.EXECUTE_SQL
        or action.contract_id != contract.contract_id
        or observation.contract_id != contract.contract_id
        or action.hypothesis_id != contract.hypothesis_id
        or observation.hypothesis_id != contract.hypothesis_id
    ):
        return _invalid(
            observation, contract, validation_fingerprint, "observation_link_mismatch"
        )
    if not observation.ok:
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            _FAILED_TOOL_ERRORS.get(observation.safe_error or "", "unknown_tool_error"),
        )
    if observation.possibly_truncated and not contract.allow_truncation:
        return _invalid(
            observation, contract, validation_fingerprint, "result_truncated"
        )

    restored = _restore_query_result(observation, contract, validation_fingerprint)
    if isinstance(restored, ObservationValidation):
        return restored
    result = restored
    if result.columns != contract.column_names:
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            "column_contract_mismatch",
            repairable=True,
        )
    if not contract.min_rows <= result.row_count <= contract.max_rows:
        error_code = (
            "limit_exceeded"
            if contract.limit is not None and result.row_count > contract.limit
            else "row_count_mismatch"
        )
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            error_code,
            repairable=True,
        )
    if contract.limit is not None and result.row_count > contract.limit:
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            "limit_exceeded",
            repairable=True,
        )

    for row in result.rows:
        for value, column in zip(row, contract.columns, strict=True):
            if value is None and not column.nullable:
                return _invalid(
                    observation,
                    contract,
                    validation_fingerprint,
                    "nullable_contract_mismatch",
                    repairable=True,
                )
            if not _valid_type(value, column):
                return _invalid(
                    observation,
                    contract,
                    validation_fingerprint,
                    "type_contract_mismatch",
                    repairable=True,
                )

    ratio_error = _ratio_policy_error(contract, result)
    if ratio_error is not None:
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            ratio_error,
            repairable=True,
        )

    indexes = {name: index for index, name in enumerate(result.columns)}
    keys = tuple(tuple(row[indexes[name]] for name in contract.key_columns) for row in result.rows)
    if len(keys) != len(set(keys)):
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            "key_not_unique",
            repairable=True,
        )
    if contract.order_by and not _ordered(result.rows, contract):
        return _invalid(
            observation,
            contract,
            validation_fingerprint,
            "order_contract_mismatch",
            repairable=True,
        )
    return ObservationValidation(
        observation_id=observation.observation_id,
        contract_id=contract.contract_id,
        validation_fingerprint=validation_fingerprint,
        valid=True,
    )


def _evidence_stance(
    contract: ObservationContract,
    result: QueryResult,
) -> Literal["supports", "refutes"]:
    if contract.contract_id != "gmv_comparison":
        return "supports"
    indexes = {name: index for index, name in enumerate(result.columns)}
    current = _decimal(result.rows[0][indexes["current_gmv"]])
    previous = _decimal(result.rows[0][indexes["previous_gmv"]])
    if current is not None and previous is not None and current < previous:
        return "supports"
    return "refutes"


def extract_evidence(
    action: AgentAction,
    observation: Observation,
    validation: ObservationValidation,
    contract: ObservationContract,
) -> tuple[EvidenceItem, ...]:
    """Project numeric claims only after a valid, one-to-one Execute validation."""

    recomputed = validate_observation(action, observation, contract)
    if (
        recomputed != validation
        or not recomputed.valid
        or validation.observation_id != observation.observation_id
        or validation.contract_id != contract.contract_id
        or action.action_type is not ActionType.EXECUTE_SQL
        or observation.tool_name is not ActionType.EXECUTE_SQL
        or not observation.ok
        or not (
            action.contract_id == observation.contract_id == contract.contract_id
        )
        or action.hypothesis_id != observation.hypothesis_id
        or observation.hypothesis_id != contract.hypothesis_id
        or observation.query_id is None
    ):
        return ()
    restored = _restore_query_result(
        observation, contract, recomputed.validation_fingerprint
    )
    if isinstance(restored, ObservationValidation):
        return ()

    dimension_indexes = tuple(
        (index, column.name)
        for index, column in enumerate(contract.columns)
        if column.role in {"dimension", "identifier", "period"}
    )
    stance = _evidence_stance(contract, restored)
    evidence: list[EvidenceItem] = []
    for row_index, row in enumerate(restored.rows):
        dimensions = tuple((name, str(row[index])) for index, name in dimension_indexes)
        for column_index, column in enumerate(contract.columns):
            if column.role != "metric" or row[column_index] is None:
                continue
            numeric_value = _decimal(row[column_index])
            evidence.append(
                EvidenceItem(
                    evidence_id=(
                        "evidence-"
                        + sha256(
                            (
                                f"{observation.observation_id}:"
                                f"{observation.query_id}:"
                                f"{recomputed.validation_fingerprint}:"
                                f"{row_index}:{column.name}"
                            ).encode()
                        ).hexdigest()
                    ),
                    observation_id=observation.observation_id,
                    hypothesis_id=contract.hypothesis_id,
                    contract_id=contract.contract_id,
                    query_id=observation.query_id,
                    claim_key=column.name,
                    dimensions=dimensions,
                    stance=stance,
                    numeric_value=numeric_value,
                    unit=column.unit if numeric_value is not None else None,
                    verified=True,
                )
            )
    return tuple(evidence)


def _attribution_completion(
    evidence: tuple[EvidenceItem, ...],
) -> tuple[bool, bool, tuple[str, ...]]:
    supported = {
        item.hypothesis_id
        for item in evidence
        if item.verified and item.stance == "supports"
    }
    if "confirm_decline" not in supported:
        return False, False, ("confirm_decline",)
    completed = supported & _ATTRIBUTION_DIMENSIONS
    if len(completed) == 3:
        return True, False, ()
    if completed:
        return False, True, tuple(sorted(_ATTRIBUTION_DIMENSIONS - completed))
    return False, False, tuple(sorted(_ATTRIBUTION_DIMENSIONS))


def assess_evidence(
    plan: TypedMetricPlan,
    evidence: tuple[EvidenceItem, ...],
) -> EvidenceAssessment:
    """Assess only verified production evidence against hypotheses in the typed plan."""

    verified = tuple(item for item in evidence if item.verified)
    resolved = tuple(sorted({item.hypothesis_id for item in verified}))
    if plan.analysis_type.value == "attribution":
        comparison = tuple(
            item for item in verified if item.hypothesis_id == "confirm_decline"
        )
        if comparison and all(item.stance == "refutes" for item in comparison):
            return EvidenceAssessment(
                complete=False,
                partial=False,
                premise_not_met=True,
                verified_count=len(verified),
                resolved_hypotheses=resolved,
                gaps=(),
                stop_reason=StopReason.PREMISE_NOT_MET,
            )
        complete, partial, gaps = _attribution_completion(verified)
    else:
        required = {item.hypothesis_id for item in plan.hypotheses}
        completed = required & set(resolved)
        gaps = tuple(sorted(required - completed))
        complete = completed == required
        partial = bool(completed) and not complete
    return EvidenceAssessment(
        complete=complete,
        partial=partial,
        premise_not_met=False,
        verified_count=len(verified),
        resolved_hypotheses=resolved,
        gaps=gaps,
        stop_reason=(
            StopReason.ANSWER_COMPLETE
            if complete
            else StopReason.EVIDENCE_PARTIAL
            if partial
            else None
        ),
    )


def _blocked_reason(error_code: str | None) -> StopReason:
    mapping = {
        "database_error": StopReason.DATABASE_ERROR,
        "policy_blocked": StopReason.SQL_POLICY_REJECTED,
        "result_truncated": StopReason.RESULT_TRUNCATED,
        "sensitive_result_blocked": StopReason.SENSITIVE_RESULT_BLOCKED,
        "sql_timeout": StopReason.SQL_TIMEOUT,
        "unknown_tool_error": StopReason.INTERNAL_ERROR,
    }
    if error_code is None:
        return StopReason.ANSWER_CONTRACT_UNMET
    return mapping.get(error_code, StopReason.ANSWER_CONTRACT_UNMET)


def repair_decision(
    validation: ObservationValidation,
    repair_history: tuple[RepairRecord, ...],
    governance: GovernanceSnapshot,
) -> RepairDecision:
    """Allow one governed result-shape repair across the shared repair history."""

    error_code = validation.error_code
    if (
        validation.repairable
        and error_code in _REPAIRABLE_ERRORS
        and governance.repair_count == 0
        and not repair_history
    ):
        return RepairDecision(allowed=True, error_code=error_code)
    if validation.repairable and error_code in _REPAIRABLE_ERRORS:
        return RepairDecision(
            allowed=False,
            error_code=error_code,
            stop_reason=StopReason.REPAIR_FAILED,
        )
    return RepairDecision(
        allowed=False,
        error_code=error_code,
        stop_reason=_blocked_reason(error_code),
    )


__all__ = [
    "assess_evidence",
    "extract_evidence",
    "repair_decision",
    "validate_observation",
]
