"""Add public rental listing inventory.

Revision ID: 0006_rental_core
Revises: 0005_sale_backfill
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_rental_core"
down_revision: str | None = "0005_sale_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rental_properties",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_property_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=255)),
        sa.Column("monthly_rent_twd", sa.BigInteger(), nullable=False),
        sa.Column("rent_per_ping_twd", sa.BigInteger(), nullable=False),
        sa.Column("area_ping", sa.Numeric(10, 2), nullable=False),
        sa.Column("layout", sa.String(length=32)),
        sa.Column("floor", sa.String(length=32)),
        sa.Column("total_floors", sa.Integer()),
        sa.Column("rental_type", sa.String(length=32)),
        sa.Column("landlord_type", sa.String(length=32)),
        sa.Column("features", sa.Text()),
        sa.Column("is_backfill", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("district_median_rent_per_ping_twd", sa.BigInteger()),
        sa.Column("discount_rate", sa.Numeric(7, 4)),
        sa.Column("score", sa.Integer()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_property_id", name="uq_rental_source_id"),
    )
    for column in ("source", "city", "district", "monthly_rent_twd", "score", "status"):
        op.create_index(f"ix_rental_properties_{column}", "rental_properties", [column])
    op.create_table(
        "rental_price_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "rental_property_id",
            sa.Integer(),
            sa.ForeignKey("rental_properties.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("monthly_rent_twd", sa.BigInteger(), nullable=False),
        sa.Column("rent_per_ping_twd", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_rental_price_history_rental_property_id",
        "rental_price_history",
        ["rental_property_id"],
    )


def downgrade() -> None:
    op.drop_table("rental_price_history")
    op.drop_table("rental_properties")
