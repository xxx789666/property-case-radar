"""Add natural-language rental subscriptions and outbox linkage.

Revision ID: 0007_rental_subscriptions
Revises: 0006_rental_core
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_rental_subscriptions"
down_revision: str | None = "0006_rental_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rental_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32)),
        sa.Column("max_monthly_rent_twd", sa.BigInteger()),
        sa.Column("min_area_ping", sa.Numeric(10, 2)),
        sa.Column("max_area_ping", sa.Numeric(10, 2)),
        sa.Column("rental_type", sa.String(length=32)),
        sa.Column("layout_contains", sa.String(length=32)),
        sa.Column("features_contains", sa.String(length=64)),
        sa.Column("min_score", sa.Integer()),
        sa.Column("channel_id", sa.BigInteger()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_rental_subscriptions_discord_user_id",
        "rental_subscriptions",
        ["discord_user_id"],
    )
    with op.batch_alter_table("notification_logs") as batch_op:
        batch_op.add_column(
            sa.Column("rental_property_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_notification_logs_rental_property_id_rental_properties",
            "rental_properties",
            ["rental_property_id"],
            ["id"],
        )
    op.create_index(
        "ix_notification_logs_rental_property_id",
        "notification_logs",
        ["rental_property_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_logs_rental_property_id",
        table_name="notification_logs",
    )
    with op.batch_alter_table("notification_logs") as batch_op:
        batch_op.drop_constraint(
            "fk_notification_logs_rental_property_id_rental_properties",
            type_="foreignkey",
        )
        batch_op.drop_column("rental_property_id")
    op.drop_table("rental_subscriptions")
