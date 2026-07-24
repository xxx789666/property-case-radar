"""sale and shared core tables

Revision ID: 0001
Revises:

Deliberately does NOT import database.models / call
Base.metadata.create_all(). A migration that derives its DDL from the
live ORM metadata is not deterministic: if a model gains/loses a column
later, this revision's *historical* behavior would silently change too
(e.g. re-running `alembic upgrade` against an old database, or reasoning
about what revision 0001 actually produced, would depend on whatever the
models look like today rather than what they looked like at 0001). Every
column/index/constraint below is a frozen snapshot, hand-matched to
database/models/{base,common,sale}.py as they existed when this revision
was written. auction_* tables and notification_logs.auction_case_id are
intentionally NOT created here -- see revision 0002.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_users_discord_user_id", "users", ["discord_user_id"], unique=True)

    op.create_table(
        "discord_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("purpose", name="uq_discord_channels_purpose"),
        sa.UniqueConstraint("discord_channel_id", name="uq_discord_channels_discord_channel_id"),
    )

    op.create_table(
        "actual_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("total_price_twd", sa.BigInteger(), nullable=False),
        sa.Column("building_area_ping", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_price_per_ping_twd", sa.BigInteger(), nullable=False),
        sa.Column("building_type", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_actual_transactions_city", "actual_transactions", ["city"])
    op.create_index("ix_actual_transactions_district", "actual_transactions", ["district"])
    op.create_index("ix_actual_transactions_transaction_date", "actual_transactions", ["transaction_date"])
    op.create_index(
        "ix_actual_transactions_unit_price_per_ping_twd", "actual_transactions", ["unit_price_per_ping_twd"]
    )

    op.create_table(
        "market_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32), nullable=False),
        sa.Column("building_type", sa.String(length=64), nullable=False),
        sa.Column("average_unit_price_twd", sa.BigInteger(), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.UniqueConstraint("city", "district", "building_type", name="uq_market_area_type"),
    )
    op.create_index("ix_market_prices_city", "market_prices", ["city"])
    op.create_index("ix_market_prices_district", "market_prices", ["district"])

    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_property_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("total_price_twd", sa.BigInteger(), nullable=False),
        sa.Column("unit_price_per_ping_twd", sa.BigInteger(), nullable=False),
        sa.Column("building_area_ping", sa.Numeric(10, 2), nullable=False),
        sa.Column("land_area_ping", sa.Numeric(10, 2), nullable=True),
        sa.Column("age_years", sa.Numeric(6, 2), nullable=True),
        sa.Column("floor", sa.String(length=32), nullable=True),
        sa.Column("total_floors", sa.Integer(), nullable=True),
        sa.Column("layout", sa.String(length=32), nullable=True),
        sa.Column("building_type", sa.String(length=64), nullable=True),
        sa.Column("usage", sa.String(length=64), nullable=True),
        sa.Column("has_parking", sa.Boolean(), nullable=True),
        sa.Column("listed_date", sa.Date(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_unit_price_twd", sa.BigInteger(), nullable=True),
        sa.Column("discount_rate", sa.Numeric(7, 4), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.UniqueConstraint("source", "source_property_id", name="uq_property_source_id"),
    )
    op.create_index("ix_properties_source", "properties", ["source"])
    op.create_index("ix_properties_city", "properties", ["city"])
    op.create_index("ix_properties_district", "properties", ["district"])
    op.create_index("ix_properties_total_price_twd", "properties", ["total_price_twd"])
    op.create_index("ix_properties_score", "properties", ["score"])
    op.create_index("ix_properties_status", "properties", ["status"])

    op.create_table(
        "property_price_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "property_id", sa.Integer(), sa.ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("total_price_twd", sa.BigInteger(), nullable=False),
        sa.Column("unit_price_per_ping_twd", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_property_price_history_property_id", "property_price_history", ["property_id"])

    op.create_table(
        "property_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32), nullable=True),
        sa.Column("max_total_price_twd", sa.BigInteger(), nullable=True),
        sa.Column("min_building_area_ping", sa.Numeric(10, 2), nullable=True),
        sa.Column("max_age_years", sa.Numeric(6, 2), nullable=True),
        sa.Column("min_discount_rate", sa.Numeric(7, 4), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_property_subscriptions_discord_user_id", "property_subscriptions", ["discord_user_id"])

    op.create_table(
        "notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("property_id", sa.Integer(), sa.ForeignKey("properties.id"), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    # notification_logs.auction_case_id is added by revision 0002, once
    # the auction_cases table it references exists -- this revision only
    # knows about the sale + shared schema as it existed at 0001.


def downgrade() -> None:
    op.drop_table("notification_logs")
    op.drop_table("property_subscriptions")
    op.drop_table("property_price_history")
    op.drop_table("properties")
    op.drop_table("market_prices")
    op.drop_table("actual_transactions")
    op.drop_table("discord_channels")
    op.drop_table("users")
