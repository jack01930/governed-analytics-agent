from __future__ import annotations

from pathlib import Path

import pytest

from governed_analytics.evals.context import (
    EVALUATION_CONTEXT_VERSION,
    build_metric_context,
    build_schema_context,
)


def test_contexts_are_stable_complete_and_do_not_leak_golden_truth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    schema = build_schema_context()
    metrics = build_metric_context()

    assert schema == build_schema_context()
    assert metrics == build_metric_context()
    assert EVALUATION_CONTEXT_VERSION == "ecommerce-schema-v1+metrics-v1"
    for table in (
        "categories",
        "customers",
        "products",
        "orders",
        "order_items",
        "payments",
        "refunds",
        "inventory_snapshots",
        "web_sessions",
        "marketing_campaigns",
        "campaign_attributions",
        "pipeline_runs",
    ):
        assert f"TABLE {table}" in schema
    assert "orders.status enum: placed, paid, completed, cancelled, refunded" in schema
    assert "orders.channel enum: organic, search, social, affiliate, email" in schema
    for pseudo_column in ("status in", "channel in", "and payable", "on delete"):
        assert pseudo_column not in schema
    assert metrics.count("METRIC ") == 15
    assert "METRIC gmv version=1.0.0" in metrics
    for prohibited in ("oracle", "expected", "anomaly", "reset_dataset"):
        assert prohibited not in (schema + metrics).lower()
