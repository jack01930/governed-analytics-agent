"""Add a narrowly scoped privileged reset for the synthetic dataset loader.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Expose one fixed-table reset operation to the loader without DDL ownership."""
    op.execute("""
        create function public.reset_analytics_dataset()
        returns void
        language plpgsql
        security definer
        set search_path = pg_catalog
        as $$
        begin
          truncate table public.categories, public.customers, public.products,
            public.marketing_campaigns, public.orders, public.order_items,
            public.payments, public.refunds, public.inventory_snapshots,
            public.web_sessions, public.campaign_attributions, public.pipeline_runs
            restart identity;
        end;
        $$;

        revoke all on function public.reset_analytics_dataset() from public;
        grant execute on function public.reset_analytics_dataset() to analytics_loader;
        """)


def downgrade() -> None:
    """Remove the privileged loader reset entry point."""
    op.execute("""
        revoke execute on function public.reset_analytics_dataset() from analytics_loader;
        drop function public.reset_analytics_dataset();
        """)
