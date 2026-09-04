import json
import re
from datetime import UTC, datetime
from typing import Literal, cast

import pytest

from governed_analytics.agent.contracts import (
    ActionType,
    AgentAction,
    AnalysisType,
    AnswerContract,
    ColumnContract,
    GovernanceSnapshot,
    Hypothesis,
    JsonValue,
    Observation,
    ObservationContract,
    ObservationValidation,
    RepairRecord,
    ResultShape,
    SortKey,
    StopReason,
    TimeWindow,
    TypedMetricPlan,
)
from governed_analytics.agent.nodes.planning import compile_answer_contract
from governed_analytics.agent.validation import (
    assess_evidence,
    extract_evidence,
    repair_decision,
    validate_observation,
)
from governed_analytics.tools import MetricInfo

QUERY_ID = "a" * 64


def metric_info() -> MetricInfo:
    return MetricInfo(
        metric_id="gmv",
        name_zh="商品交易总额",
        name_en="GMV",
        description="Paid order gross merchandise value.",
        expression_sql="sum(amount)",
        time_field="ordered_at",
        default_filters=("status = 'paid'",),
        dimensions=("region", "product", "segment"),
        source_tables=("orders",),
        unit="cny",
        version="v1",
    )


def attribution_plan() -> TypedMetricPlan:
    return TypedMetricPlan(
        plan_id="gmv-attribution-plan",
        revision=1,
        metric_id="gmv",
        metric_version="v1",
        analysis_type=AnalysisType.ATTRIBUTION,
        windows=(
            TimeWindow(
                label="previous",
                start_at=datetime(2026, 6, 1, tzinfo=UTC),
                end_at=datetime(2026, 6, 8, tzinfo=UTC),
            ),
            TimeWindow(
                label="current",
                start_at=datetime(2026, 6, 8, tzinfo=UTC),
                end_at=datetime(2026, 6, 15, tzinfo=UTC),
            ),
        ),
        dimensions=("region", "product", "segment"),
        null_policy="preserve",
        zero_denominator_policy="return_null",
        fill_policy="none",
        hypotheses=(
            Hypothesis(hypothesis_id="confirm_decline", kind="confirm_decline"),
            Hypothesis(
                hypothesis_id="region_contribution",
                kind="dimension_contribution",
                dimension="region",
            ),
            Hypothesis(
                hypothesis_id="sku_contribution",
                kind="dimension_contribution",
                dimension="product",
            ),
            Hypothesis(
                hypothesis_id="segment_contribution",
                kind="dimension_contribution",
                dimension="segment",
            ),
        ),
    )


def answer_contract() -> AnswerContract:
    return compile_answer_contract(attribution_plan(), metric_info())


def action(contract_id: str, hypothesis_id: str | None = None) -> AgentAction:
    return AgentAction(
        action_type=ActionType.EXECUTE_SQL,
        purpose=contract_id,
        arguments={"sql": "select controlled_result"},
        hypothesis_id=hypothesis_id or contract_id,
        contract_id=contract_id,
        expected_evidence="contracted numeric result",
    )


def observation(
    *,
    contract_id: str = "region_contribution",
    hypothesis_id: str | None = None,
    columns: tuple[str, ...] = ("region", "gmv_loss"),
    rows: tuple[tuple[object, ...], ...] = (("华南", "12.00"),),
    query_id: str = QUERY_ID,
    truncated: bool = False,
    ok: bool = True,
    safe_error: str | None = None,
    observation_id: str = "observation-1",
) -> Observation:
    payload = None
    if ok:
        payload = {
            "query_id": query_id,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "row_limit": 500,
            "possibly_truncated": truncated,
        }
    return Observation(
        observation_id=observation_id,
        tool_name=ActionType.EXECUTE_SQL,
        purpose=contract_id,
        ok=ok,
        safe_error=safe_error,
        hypothesis_id=hypothesis_id or contract_id,
        contract_id=contract_id,
        query_id=query_id if ok else None,
        columns=columns if ok else (),
        row_count=len(rows) if ok else None,
        possibly_truncated=truncated,
        payload=cast(JsonValue, payload),
    )


def validated_evidence(contract_id: str, rows: tuple[tuple[object, ...], ...]):  # type: ignore[no-untyped-def]
    contract = answer_contract().contract(contract_id)
    columns = contract.column_names
    item = observation(
        contract_id=contract_id,
        hypothesis_id=contract.hypothesis_id,
        columns=columns,
        rows=rows,
        observation_id=f"observation-{contract_id}",
    )
    execute = action(contract_id, contract.hypothesis_id)
    validation = validate_observation(execute, item, contract)
    assert validation.valid
    return extract_evidence(execute, item, validation, contract)


def test_validation_separates_repairable_shape_from_nonrepairable_truncation() -> None:
    contract = answer_contract().contract("region_contribution")
    wrong_columns = observation(columns=("area", "loss"), truncated=False)
    truncated = observation(truncated=True)

    first = validate_observation(action("region_contribution"), wrong_columns, contract)
    second = validate_observation(action("region_contribution"), truncated, contract)

    assert first.repairable and first.error_code == "column_contract_mismatch"
    assert not second.repairable and second.error_code == "result_truncated"


@pytest.mark.parametrize(
    ("rows", "error_code"),
    [
        ((("华南", "12"), ("华南", "11")), "key_not_unique"),
        ((("华南", "11"), ("华北", "12")), "order_contract_mismatch"),
        (((),), "row_shape_mismatch"),
        ((("华南", None),), "nullable_contract_mismatch"),
        ((("华南", True),), "type_contract_mismatch"),
    ],
)
def test_validation_checks_key_order_shape_nullable_and_strict_types(
    rows: tuple[tuple[object, ...], ...], error_code: str
) -> None:
    contract = answer_contract().contract("region_contribution")
    result = validate_observation(
        action("region_contribution"), observation(rows=rows), contract
    )

    assert not result.valid
    assert result.repairable
    assert result.error_code == error_code


def test_validation_checks_min_max_rows_and_limit() -> None:
    contract = answer_contract().contract("region_contribution")
    empty = observation(rows=())
    too_many = observation(
        rows=tuple((f"region-{index}", str(100 - index)) for index in range(11))
    )

    assert validate_observation(action("region_contribution"), empty, contract).error_code == (
        "row_count_mismatch"
    )
    assert validate_observation(
        action("region_contribution"), too_many, contract
    ).error_code == "limit_exceeded"


def test_validation_accepts_strict_json_representations_for_all_column_types() -> None:
    contract = ObservationContract(
        contract_id="typed_contract",
        hypothesis_id="typed_result",
        columns=(
            ColumnContract(name="label", data_type="string", role="dimension"),
            ColumnContract(
                name="quantity", data_type="integer", role="metric", unit="count"
            ),
            ColumnContract(
                name="amount", data_type="decimal", role="metric", unit="cny"
            ),
            ColumnContract(name="active", data_type="boolean", role="metric"),
            ColumnContract(name="day", data_type="date", role="period"),
            ColumnContract(name="recorded_at", data_type="datetime", role="period"),
        ),
        shape=ResultShape.SINGLE_ROW,
        min_rows=1,
        max_rows=1,
        key_columns=("label",),
    )
    columns = contract.column_names
    item = observation(
        contract_id="typed_contract",
        hypothesis_id="typed_result",
        columns=columns,
        rows=(("north", 2, "1.25", True, "2026-06-01", "2026-06-01T00:00:00Z"),),
    )
    execute = action("typed_contract", "typed_result")

    assert validate_observation(execute, item, contract).valid
    invalid_integer = observation(
        contract_id="typed_contract",
        hypothesis_id="typed_result",
        columns=columns,
        rows=(("north", True, "1.25", True, "2026-06-01", "2026-06-01T00:00:00Z"),),
    )
    assert validate_observation(execute, invalid_integer, contract).error_code == (
        "type_contract_mismatch"
    )


@pytest.mark.parametrize(
    ("data_type", "rows", "valid"),
    [
        ("string", (("10",), ("2",)), True),
        ("integer", ((10,), (2,)), False),
        ("decimal", (("10",), ("2",)), False),
        ("date", (("2026-01-01",), ("2026-02-01",)), True),
        (
            "datetime",
            (
                ("2026-01-01T00:30:00+01:00",),
                ("2026-01-01T00:00:00+00:00",),
            ),
            True,
        ),
        ("boolean", ((False,), (True,)), True),
    ],
)
def test_sorting_uses_declared_column_type(
    data_type: Literal["string", "integer", "decimal", "boolean", "date", "datetime"],
    rows: tuple[tuple[object, ...], ...],
    valid: bool,
) -> None:
    numeric = data_type in {"integer", "decimal"}
    role: Literal["metric", "dimension"] = "metric" if numeric else "dimension"
    contract = ObservationContract(
        contract_id="typed_sort",
        hypothesis_id="typed_sort",
        columns=(
            ColumnContract(
                name="value",
                data_type=data_type,
                role=role,
                unit="count" if numeric else None,
            ),
        ),
        shape=ResultShape.TABLE,
        min_rows=0,
        max_rows=10,
        key_columns=("value",),
        order_by=(SortKey(column="value", direction="asc"),),
    )
    item = observation(
        contract_id="typed_sort",
        hypothesis_id="typed_sort",
        columns=("value",),
        rows=rows,
    )

    result = validate_observation(action("typed_sort"), item, contract)

    assert result.valid is valid
    assert result.error_code == (None if valid else "order_contract_mismatch")


def test_string_tie_break_accepts_only_one_stable_top_k_order() -> None:
    contract = ObservationContract(
        contract_id="stable_top_k",
        hypothesis_id="stable_top_k",
        columns=(
            ColumnContract(name="label", data_type="string", role="dimension"),
            ColumnContract(name="amount", data_type="decimal", role="metric", unit="cny"),
        ),
        shape=ResultShape.TOP_K,
        min_rows=1,
        max_rows=2,
        key_columns=("label",),
        order_by=(
            SortKey(column="amount", direction="desc"),
            SortKey(column="label", direction="asc"),
        ),
        limit=2,
    )
    stable = observation(
        contract_id="stable_top_k",
        hypothesis_id="stable_top_k",
        columns=contract.column_names,
        rows=(("10", "5"), ("2", "5")),
    )
    unstable = observation(
        contract_id="stable_top_k",
        hypothesis_id="stable_top_k",
        columns=contract.column_names,
        rows=(("2", "5"), ("10", "5")),
    )

    assert validate_observation(action("stable_top_k"), stable, contract).valid
    assert validate_observation(
        action("stable_top_k"), unstable, contract
    ).error_code == "order_contract_mismatch"


def test_grouped_table_without_order_contract_accepts_any_valid_row_order() -> None:
    contract = ObservationContract(
        contract_id="unordered_group",
        hypothesis_id="unordered_group",
        columns=(
            ColumnContract(name="region", data_type="string", role="dimension"),
            ColumnContract(name="amount", data_type="decimal", role="metric", unit="cny"),
        ),
        shape=ResultShape.TABLE,
        min_rows=0,
        max_rows=10,
        key_columns=("region",),
    )
    descending_labels = observation(
        contract_id="unordered_group",
        hypothesis_id="unordered_group",
        columns=contract.column_names,
        rows=(("z", "1"), ("a", "2")),
    )

    assert validate_observation(
        action("unordered_group"), descending_labels, contract
    ).valid


def test_ratio_requires_null_exactly_for_zero_denominator() -> None:
    contract = answer_contract().contract("gmv_comparison")
    zero = observation(
        contract_id="gmv_comparison",
        hypothesis_id="confirm_decline",
        columns=contract.column_names,
        rows=(("0", "0", None),),
    )
    invalid_zero = zero.model_copy(
        update={
            "payload": {
                "query_id": QUERY_ID,
                "columns": contract.column_names,
                "rows": (("0", "0", "0"),),
                "row_count": 1,
                "row_limit": 500,
                "possibly_truncated": False,
            }
        }
    )
    missing_ratio = observation(
        contract_id="gmv_comparison",
        hypothesis_id="confirm_decline",
        columns=contract.column_names,
        rows=(("80", "100", None),),
    )

    assert validate_observation(
        action("gmv_comparison", "confirm_decline"), zero, contract
    ).valid
    assert validate_observation(
        action("gmv_comparison", "confirm_decline"), invalid_zero, contract
    ).error_code == "zero_denominator_contract_mismatch"
    assert validate_observation(
        action("gmv_comparison", "confirm_decline"), missing_ratio, contract
    ).error_code == "nullable_contract_mismatch"


def test_validation_requires_matching_action_observation_contract_and_query_metadata() -> None:
    contract = answer_contract().contract("region_contribution")
    wrong_action = action("region_contribution").model_copy(
        update={"hypothesis_id": "sku_contribution"}
    )
    inconsistent_query = observation().model_copy(update={"query_id": "b" * 64})

    assert validate_observation(wrong_action, observation(), contract).error_code == (
        "observation_link_mismatch"
    )
    assert validate_observation(
        action("region_contribution"), inconsistent_query, contract
    ).error_code == "query_result_mismatch"


@pytest.mark.parametrize(
    ("safe_error", "expected_code"),
    [
        ("sql_timeout", "sql_timeout"),
        ("read_only_policy", "policy_blocked"),
        ("sensitive_output", "sensitive_result_blocked"),
        ("database_error", "database_error"),
        ("unrecognized_failure", "unknown_tool_error"),
    ],
)
def test_failed_execute_categories_are_stable_and_never_repairable(
    safe_error: str, expected_code: str
) -> None:
    contract = answer_contract().contract("region_contribution")
    failed = observation(ok=False, safe_error=safe_error)

    result = validate_observation(action("region_contribution"), failed, contract)

    assert not result.valid and not result.repairable
    assert result.error_code == expected_code


def test_evidence_is_emitted_only_for_one_validated_execute_observation() -> None:
    contract = answer_contract().contract("region_contribution")
    item = observation(rows=(("华南", "12"), ("华北", "10")))
    execute = action("region_contribution")
    validation = validate_observation(execute, item, contract)

    evidence = extract_evidence(execute, item, validation, contract)

    assert evidence
    assert {entry.observation_id for entry in evidence} == {item.observation_id}
    assert all(
        entry.verified
        and entry.query_id == QUERY_ID
        and entry.contract_id == "region_contribution"
        and entry.hypothesis_id == "region_contribution"
        and entry.unit
        for entry in evidence
    )
    invalid = validation.model_copy(
        update={"valid": False, "error_code": "type_contract_mismatch"}
    )
    assert extract_evidence(execute, item, invalid, contract) == ()


def test_evidence_recomputes_validation_and_rejects_forged_or_stale_markers() -> None:
    contract = answer_contract().contract("region_contribution")
    execute = action("region_contribution")
    valid_item = observation(rows=(("华南", "12"), ("华北", "10")))
    valid_marker = validate_observation(execute, valid_item, contract)
    forged = ObservationValidation(
        observation_id=valid_item.observation_id,
        contract_id=contract.contract_id,
        validation_fingerprint="f" * 64,
        valid=True,
    )
    wrong_columns = observation(
        columns=("area", "loss"),
        rows=(("华南", "12"),),
        observation_id=valid_item.observation_id,
    )
    wrong_order = observation(
        rows=(("华南", "10"), ("华北", "12")),
        observation_id=valid_item.observation_id,
    )
    truncated = observation(
        truncated=True,
        observation_id=valid_item.observation_id,
    )

    assert extract_evidence(execute, valid_item, valid_marker, contract)
    assert extract_evidence(execute, wrong_columns, forged, contract) == ()
    assert extract_evidence(execute, wrong_order, valid_marker, contract) == ()
    assert extract_evidence(execute, truncated, valid_marker, contract) == ()


def test_validation_fingerprint_binds_all_current_valid_inputs_and_evidence_id() -> None:
    contract = answer_contract().contract("region_contribution")
    execute = action("region_contribution")
    original = observation(rows=(("华南", "12"), ("华北", "10")))
    original_marker = validate_observation(execute, original, contract)
    original_evidence = extract_evidence(execute, original, original_marker, contract)

    changed_query = observation(
        query_id="b" * 64,
        rows=(("华南", "12"), ("华北", "10")),
        observation_id=original.observation_id,
    )
    changed_query_marker = validate_observation(execute, changed_query, contract)
    changed_query_evidence = extract_evidence(
        execute, changed_query, changed_query_marker, contract
    )

    changed_rows = observation(
        rows=(("华南", "13"), ("华北", "9")),
        observation_id=original.observation_id,
    )
    changed_rows_marker = validate_observation(execute, changed_rows, contract)
    changed_rows_evidence = extract_evidence(
        execute, changed_rows, changed_rows_marker, contract
    )

    private_action_sentinel = "private_action_sentinel"
    changed_action = execute.model_copy(
        update={
            "purpose": "changed purpose",
            "arguments": {"sql": private_action_sentinel},
        }
    )
    changed_action_marker = validate_observation(changed_action, original, contract)
    changed_action_evidence = extract_evidence(
        changed_action, original, changed_action_marker, contract
    )

    assert original_evidence and changed_query_evidence
    assert changed_rows_evidence and changed_action_evidence
    assert extract_evidence(execute, changed_query, original_marker, contract) == ()
    assert extract_evidence(execute, changed_rows, original_marker, contract) == ()
    assert extract_evidence(changed_action, original, original_marker, contract) == ()
    fingerprints = {
        original_marker.validation_fingerprint,
        changed_query_marker.validation_fingerprint,
        changed_rows_marker.validation_fingerprint,
        changed_action_marker.validation_fingerprint,
    }
    assert len(fingerprints) == 4
    assert all(re.fullmatch(r"[0-9a-f]{64}", item) for item in fingerprints)
    evidence_ids = {
        original_evidence[0].evidence_id,
        changed_query_evidence[0].evidence_id,
        changed_rows_evidence[0].evidence_id,
        changed_action_evidence[0].evidence_id,
    }
    assert len(evidence_ids) == 4
    rendered = json.dumps(
        {
            "fingerprints": tuple(fingerprints),
            "evidence_ids": tuple(evidence_ids),
        }
    )
    assert private_action_sentinel not in rendered
    assert "华南" not in rendered


def test_evidence_uses_contract_units_without_column_name_guessing() -> None:
    contract = answer_contract().contract("segment_contribution")
    item = observation(
        contract_id=contract.contract_id,
        columns=contract.column_names,
        rows=(("new", "50", "40", "-10"),),
    )
    execute = action(contract.contract_id)
    validation = validate_observation(execute, item, contract)

    evidence = extract_evidence(execute, item, validation, contract)

    assert {entry.claim_key: entry.unit for entry in evidence} == {
        "previous_gmv": "cny",
        "current_gmv": "cny",
        "delta": "cny",
    }


def test_attribution_assessment_requires_decline_then_all_three_dimensions() -> None:
    decline = validated_evidence("gmv_comparison", (("80", "100", "-0.2"),))
    region = validated_evidence("region_contribution", (("华南", "12"),))
    sku = validated_evidence("sku_contribution", (("sku-1", "8"),))
    segment = validated_evidence(
        "segment_contribution", (("new", "50", "40", "-10"),)
    )

    only_decline = assess_evidence(attribution_plan(), decline)
    partial = assess_evidence(attribution_plan(), decline + region)
    complete = assess_evidence(attribution_plan(), decline + region + sku + segment)

    assert not only_decline.complete and not only_decline.partial
    assert only_decline.gaps == (
        "region_contribution",
        "segment_contribution",
        "sku_contribution",
    )
    assert partial.partial and partial.stop_reason is StopReason.EVIDENCE_PARTIAL
    assert partial.gaps == ("segment_contribution", "sku_contribution")
    assert complete.complete and complete.stop_reason is StopReason.ANSWER_COMPLETE
    assert complete.gaps == ()


def test_non_decline_is_completed_premise_not_met_without_dimensions() -> None:
    no_decline = validated_evidence("gmv_comparison", (("100", "100", "0"),))

    assessment = assess_evidence(attribution_plan(), no_decline)

    assert assessment.premise_not_met
    assert assessment.stop_reason is StopReason.PREMISE_NOT_MET
    assert assessment.gaps == ()


def test_repair_decision_obeys_error_allowlist_and_shared_budget_history() -> None:
    contract = answer_contract().contract("region_contribution")
    repairable = validate_observation(
        action("region_contribution"), observation(columns=("area", "loss")), contract
    )
    truncated = validate_observation(
        action("region_contribution"), observation(truncated=True), contract
    )
    history = (
        RepairRecord(
            repair_id="repair-1",
            kind="structured_output",
            target="plan",
            error_code="invalid_json",
            outcome="success",
        ),
    )

    allowed = repair_decision(repairable, (), GovernanceSnapshot())
    blocked_by_history = repair_decision(repairable, history, GovernanceSnapshot(repair_count=1))
    blocked_truncation = repair_decision(truncated, (), GovernanceSnapshot())

    assert allowed.allowed and allowed.error_code == "column_contract_mismatch"
    assert not blocked_by_history.allowed
    assert blocked_by_history.stop_reason is StopReason.REPAIR_FAILED
    assert not blocked_truncation.allowed
    assert blocked_truncation.stop_reason is StopReason.RESULT_TRUNCATED
