"""Static guardrails for the privileged dataset reset migration."""

from pathlib import Path


def test_reset_function_is_fixed_scope_and_defensive() -> None:
    migration = Path("migrations/versions/0003_add_dataset_reset_function.py").read_text(
        encoding="utf-8"
    ).lower()
    expected_tables = (
        "public.categories",
        "public.customers",
        "public.products",
        "public.marketing_campaigns",
        "public.orders",
        "public.order_items",
        "public.payments",
        "public.refunds",
        "public.inventory_snapshots",
        "public.web_sessions",
        "public.campaign_attributions",
        "public.pipeline_runs",
    )

    assert "create function public.reset_analytics_dataset()" in migration
    assert "security definer" in migration
    assert "set search_path = pg_catalog" in migration
    assert all(table in migration for table in expected_tables)
    assert "restart identity" in migration
    assert "cascade" not in migration
    assert "execute format" not in migration
    assert (
        "grant execute on function public.reset_analytics_dataset() "
        "to analytics_loader" in migration
    )
    assert "revoke all on function public.reset_analytics_dataset() from public" in migration
