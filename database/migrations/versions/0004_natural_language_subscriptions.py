"""Add natural-language sale subscription routing fields.

Revision ID: 0004_nl_subscriptions
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_nl_subscriptions"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "property_subscriptions",
        sa.Column("property_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "property_subscriptions",
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("property_subscriptions", "channel_id")
    op.drop_column("property_subscriptions", "property_type")
