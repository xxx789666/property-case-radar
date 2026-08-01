"""Distinguish baseline inventory from newly discovered sale listings.

Revision ID: 0005_sale_backfill
Revises: 0004_nl_subscriptions
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_sale_backfill"
down_revision = "0004_nl_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "properties",
        sa.Column(
            "is_backfill",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("properties", "is_backfill")
