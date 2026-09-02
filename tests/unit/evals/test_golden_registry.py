from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
import yaml  # type: ignore[import-untyped]
from sqlglot import exp

from governed_analytics.evals.golden import load_golden_cases

CASES_PATH = "evals/datasets/golden/cases.yaml"
EXPECTED_IDS = tuple(f"G{number:03d}" for number in range(1, 21))


def test_week_one_registry_has_twenty_unique_ordered_cases() -> None:
    cases = load_golden_cases(CASES_PATH)

    assert len(cases) == 20
    assert tuple(case.case_id for case in cases) == EXPECTED_IDS
    assert len({case.question for case in cases}) == 20
    assert all(case.oracle_sql_path.is_file() for case in cases)


def test_registry_pins_comparison_metadata_and_oracle_output_contracts() -> None:
    cases = load_golden_cases(CASES_PATH)

    assert [
        (case.case_id, case.comparison, case.key_columns, case.numeric_columns) for case in cases
    ] == [
        ("G001", "scalar", (), ("gmv",)),
        ("G002", "table", (), ("current_gmv", "previous_gmv", "change_rate")),
        ("G003", "top_k", ("region",), ("gmv_loss",)),
        ("G004", "top_k", ("sku",), ("gmv_loss",)),
        ("G005", "table", ("segment",), ("previous_gmv", "current_gmv", "delta")),
        ("G006", "table", ("cause_type",), ("previous", "current", "delta")),
        ("G007", "scalar", (), ("paid_gmv",)),
        ("G008", "scalar", (), ("net_revenue",)),
        ("G009", "top_k", ("category_code",), ("refund_rate",)),
        ("G010", "top_k", ("reason",), ("refund_count", "refund_amount")),
        ("G011", "table", ("channel",), ("conversion_rate",)),
        ("G012", "table", ("sku",), ("stockout_days",)),
        ("G013", "scalar", (), ("active_customers",)),
        ("G014", "scalar", (), ("new_customers",)),
        ("G015", "scalar", (), ("repeat_purchase_rate",)),
        ("G016", "top_k", ("campaign_code",), ("roi",)),
        ("G017", "boolean", (), ()),
        ("G018", "scalar", (), ("duplicate_rows",)),
        ("G019", "scalar", (), ("mismatched_orders",)),
        ("G020", "scalar", (), ("over_refunded_orders",)),
    ]


def test_oracles_have_read_only_explicit_and_ordered_shapes() -> None:
    cases = load_golden_cases(CASES_PATH)
    multi_row_ids = {"G003", "G004", "G005", "G006", "G009", "G010", "G011", "G012", "G016"}

    for case in cases:
        sql = case.oracle_sql_path.read_text(encoding="utf-8")
        statements = sqlglot.parse(sql, read="postgres")

        assert sql.splitlines()[0] == f"-- {case.case_id} metric_version=1.0.0"
        assert len(statements) == 1
        assert isinstance(statements[0], exp.Query)
        assert not list(statements[0].find_all(exp.Into))
        assert isinstance(statements[0], exp.Select)
        aliases = tuple(expression.alias for expression in statements[0].expressions)
        assert all(isinstance(expression, exp.Alias) for expression in statements[0].expressions)
        assert len(aliases) == len(set(aliases))
        if case.case_id in multi_row_ids:
            assert statements[0].args.get("order") is not None


def test_critical_oracles_preserve_governed_metric_semantics() -> None:
    sql_by_id = {
        case_id: (Path("evals/datasets/golden/sql") / f"{case_id}.sql").read_text(encoding="utf-8")
        for case_id in ("G006", "G009", "G016", "G019", "G020")
    }

    assert (
        "coalesce(sku_gmv.current_value, 0) - coalesce(sku_gmv.previous_value, 0)"
        in sql_by_id["G006"]
    )
    assert all(
        token in sql_by_id["G006"] for token in ("south_conversion", "SKU-000001", "SKU-000002")
    )
    assert (
        "select distinct c.category_code as category_code, o.order_id as order_id"
        in sql_by_id["G009"]
    )
    assert "join order_items as oi on oi.order_item_id = r.order_item_id" in sql_by_id["G009"]
    assert "group by p.order_id" in sql_by_id["G009"]
    assert "mc.start_at < timestamptz '2026-07-01T00:00:00Z'" in sql_by_id["G016"]
    assert "mc.end_at > timestamptz '2026-04-01T00:00:00Z'" in sql_by_id["G016"]
    assert "nullif(q2_campaigns.spend, 0)" in sql_by_id["G016"]
    assert "oit.item_net_amount + o.shipping_amount" in sql_by_id["G019"]
    assert "r.refunded_at >= timestamptz '2026-05-20T00:00:00Z'" in sql_by_id["G020"]


def test_g006_has_fixed_evidence_keys_and_governed_g015_g017_shapes() -> None:
    sql_by_id = {
        case_id: (Path("evals/datasets/golden/sql") / f"{case_id}.sql").read_text(encoding="utf-8")
        for case_id in ("G006", "G015", "G017")
    }

    assert (
        "values ('south_conversion', 1), ('SKU-000001', 2), ('SKU-000002', 3)" in sql_by_id["G006"]
    )
    assert "left join sku_gmv" in sql_by_id["G006"]
    assert "coalesce(sku_gmv.previous_value, 0)" in sql_by_id["G006"]
    assert "active_customers as" in sql_by_id["G015"]
    assert "o.ordered_at >= timestamptz '2026-06-01T00:00:00Z'" in sql_by_id["G015"]
    assert "lifetime_orders as" in sql_by_id["G015"]
    assert "count(lifetime_orders.customer_id)::numeric" in sql_by_id["G015"]
    assert "coalesce((select" in sql_by_id["G017"]
    assert "), true) as is_stale" in sql_by_id["G017"]


def test_registry_is_independent_of_current_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    cases = load_golden_cases(CASES_PATH)

    assert cases[0].oracle_sql_path == (
        Path(__file__).resolve().parents[3] / "evals/datasets/golden/sql/G001.sql"
    )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda payload: payload.__setitem__(0, {**payload[0], "case_id": "bad"}), "case ID"),
        (
            lambda payload: payload.__setitem__(1, {**payload[1], "case_id": "G001"}),
            "duplicate",
        ),
        (
            lambda payload: payload.__setitem__(0, {**payload[0], "unexpected": True}),
            "Extra inputs",
        ),
        (
            lambda payload: payload.__setitem__(2, {**payload[2], "key_columns": ["missing"]}),
            "output alias",
        ),
    ],
)
def test_registry_rejects_invalid_case_metadata(
    tmp_path: Path, mutator: object, message: str
) -> None:
    payload = yaml.safe_load(Path(CASES_PATH).read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert callable(mutator)
    mutator(payload)
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_golden_cases(cases_path)


@pytest.mark.parametrize(
    "invalid_text",
    [
        "- case_id: G001\n  category: metric\n  category: secret-category\n",
        "- case_id: G001\n  metadata:\n    version: 1\n    version: secret-version\n",
    ],
)
def test_registry_rejects_duplicate_yaml_mapping_keys(tmp_path: Path, invalid_text: str) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(invalid_text, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid golden registry YAML") as error:
        load_golden_cases(cases_path)

    assert "secret" not in str(error.value)


@pytest.mark.parametrize("field", ["question", "category"])
def test_registry_rejects_blank_versioned_text(tmp_path: Path, field: str) -> None:
    payload = yaml.safe_load(Path(CASES_PATH).read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    payload[0][field] = " \t "
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="must not be blank"):
        load_golden_cases(cases_path)


def test_registry_rejects_oracle_with_multiple_statements(tmp_path: Path) -> None:
    source_payload = yaml.safe_load(Path(CASES_PATH).read_text(encoding="utf-8"))
    assert isinstance(source_payload, list)
    sql_path = tmp_path / "G001.sql"
    sql_path.write_text(
        "-- G001 metric_version=1.0.0\nselect 1 as gmv; select 2 as gmv;\n",
        encoding="utf-8",
    )
    source_payload[0]["oracle_sql_path"] = str(sql_path)
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump(source_payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one"):
        load_golden_cases(cases_path)


def test_registry_rejects_select_into(tmp_path: Path) -> None:
    source_payload = yaml.safe_load(Path(CASES_PATH).read_text(encoding="utf-8"))
    assert isinstance(source_payload, list)
    sql_path = tmp_path / "G001.sql"
    sql_path.write_text(
        "-- G001 metric_version=1.0.0\nselect 1 as gmv into temporary result;\n",
        encoding="utf-8",
    )
    source_payload[0]["oracle_sql_path"] = str(sql_path)
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump(source_payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden node"):
        load_golden_cases(cases_path)


def test_registry_rejects_data_modifying_cte_and_missing_key_tie_breaker(tmp_path: Path) -> None:
    source_payload = yaml.safe_load(Path(CASES_PATH).read_text(encoding="utf-8"))
    assert isinstance(source_payload, list)
    dml_path = tmp_path / "G001.sql"
    dml_path.write_text(
        "-- G001 metric_version=1.0.0\n"
        "with changed as (delete from orders returning order_id) select 1 as gmv;\n",
        encoding="utf-8",
    )
    source_payload[0]["oracle_sql_path"] = str(dml_path)
    dml_cases_path = tmp_path / "dml-cases.yaml"
    dml_cases_path.write_text(yaml.safe_dump(source_payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden node"):
        load_golden_cases(dml_cases_path)

    source_payload = yaml.safe_load(Path(CASES_PATH).read_text(encoding="utf-8"))
    assert isinstance(source_payload, list)
    unordered_path = tmp_path / "G003.sql"
    unordered_path.write_text(
        "-- G003 metric_version=1.0.0\n"
        "select 'north' as region, 1 as gmv_loss order by gmv_loss desc;\n",
        encoding="utf-8",
    )
    source_payload[2]["oracle_sql_path"] = str(unordered_path)
    unordered_cases_path = tmp_path / "unordered-cases.yaml"
    unordered_cases_path.write_text(
        yaml.safe_dump(source_payload, allow_unicode=True), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="ORDER BY must cover key columns"):
        load_golden_cases(unordered_cases_path)
