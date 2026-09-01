"""Grant least-privilege access to analytics roles.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Grant loader DML and readonly query access without DDL rights."""
    op.execute("""
        grant usage on schema public to analytics_loader, analytics_readonly;
        grant select, insert, update, delete, truncate on all tables in schema public
        to analytics_loader;
        grant usage, select on all sequences in schema public to analytics_loader;
        grant select on all tables in schema public to analytics_readonly;

        alter default privileges in schema public
        grant select, insert, update, delete, truncate on tables to analytics_loader;
        alter default privileges in schema public
        grant usage, select on sequences to analytics_loader;
        alter default privileges in schema public
        grant select on tables to analytics_readonly;
        """)


def downgrade() -> None:
    """Revoke the table and sequence privileges added by this revision."""
    op.execute("""
        alter default privileges in schema public revoke select on tables from analytics_readonly;
        alter default privileges in schema public revoke usage, select on sequences
        from analytics_loader;
        alter default privileges in schema public revoke select, insert, update, delete,
        truncate on tables from analytics_loader;
        revoke select on all tables in schema public from analytics_readonly;
        revoke usage, select on all sequences in schema public from analytics_loader;
        revoke select, insert, update, delete, truncate on all tables in schema public
        from analytics_loader;
        """)
