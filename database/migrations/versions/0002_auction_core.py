"""auction tables + notification_logs auction link

Revision ID: 0002
Revises: 0001

Deliberately does NOT import database.models / call
Base.metadata.create_all() -- see revision 0001's module docstring for
why. Every column/index/constraint below is a frozen snapshot,
hand-matched to database/models/auction.py as it existed when this
revision was written. Enum-typed columns (case_type, ownership_type,
occupancy_status, status, result, to_status, from_status) are stored as
plain VARCHAR, matching ``sqlalchemy.Enum(..., native_enum=False)`` in
the ORM models -- no native Postgres ENUM type is involved, so these
columns need no special handling across SQLite/PostgreSQL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Child-before-parent order, for a clean downgrade.
_AUCTION_TABLES_CHILD_FIRST = (
    "auction_status_history",
    "auction_documents",
    "auction_rounds",
    "auction_subscriptions",
    "auction_cases",
)


def upgrade() -> None:
    op.create_table(
        "auction_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("court_name", sa.String(length=64), nullable=False),
        sa.Column("case_number", sa.String(length=128), nullable=False),
        sa.Column("division", sa.String(length=32), nullable=False),
        sa.Column("case_type", sa.String(length=32), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("district", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("announced_date", sa.Date(), nullable=True),
        sa.Column("building_area_ping", sa.Numeric(10, 2), nullable=True),
        sa.Column("land_area_ping", sa.Numeric(10, 2), nullable=True),
        sa.Column("ownership_ratio", sa.String(length=64), nullable=False),
        sa.Column("ownership_type", sa.String(length=32), nullable=False),
        sa.Column("occupancy_status", sa.String(length=32), nullable=False),
        sa.Column("occupancy_note", sa.String(length=255), nullable=False),
        sa.Column("debtor", sa.String(length=255), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("lease_status", sa.String(length=128), nullable=False),
        sa.Column("seizure_status", sa.String(length=128), nullable=False),
        sa.Column("other_encumbrances", sa.String(length=255), nullable=False),
        sa.Column("building_use", sa.String(length=64), nullable=False),
        sa.Column("zoning", sa.String(length=64), nullable=False),
        sa.Column("has_unregistered_addition", sa.Boolean(), nullable=False),
        sa.Column("announcement_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("surface_discount_rate", sa.Numeric(7, 4), nullable=True),
        sa.Column("risk_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("investment_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("court_name", "case_number", name="uq_auction_case_court_number"),
    )
    op.create_index("ix_auction_cases_court_name", "auction_cases", ["court_name"])
    op.create_index("ix_auction_cases_case_number", "auction_cases", ["case_number"])
    op.create_index("ix_auction_cases_city", "auction_cases", ["city"])
    op.create_index("ix_auction_cases_district", "auction_cases", ["district"])

    op.create_table(
        "auction_rounds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "case_id", sa.Integer(), sa.ForeignKey("auction_cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("floor_price_total_twd", sa.BigInteger(), nullable=False),
        sa.Column("floor_unit_price_twd", sa.BigInteger(), nullable=False),
        sa.Column("auction_date", sa.Date(), nullable=True),
        sa.Column("deposit_twd", sa.BigInteger(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("winning_price_twd", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("case_id", "round_number", name="uq_auction_round_case_number"),
    )
    op.create_index("ix_auction_rounds_case_id", "auction_rounds", ["case_id"])

    op.create_table(
        "auction_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "case_id", sa.Integer(), sa.ForeignKey("auction_cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auction_documents_case_id", "auction_documents", ["case_id"])
    op.create_index("ix_auction_documents_content_hash", "auction_documents", ["content_hash"])

    op.create_table(
        "auction_status_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "case_id", sa.Integer(), sa.ForeignKey("auction_cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_floor_price_total_twd", sa.BigInteger(), nullable=True),
        sa.Column("new_floor_price_total_twd", sa.BigInteger(), nullable=True),
        sa.Column("previous_floor_unit_price_twd", sa.BigInteger(), nullable=True),
        sa.Column("new_floor_unit_price_twd", sa.BigInteger(), nullable=True),
        sa.Column("previous_auction_date", sa.Date(), nullable=True),
        sa.Column("new_auction_date", sa.Date(), nullable=True),
    )
    op.create_index("ix_auction_status_history_case_id", "auction_status_history", ["case_id"])
    op.create_index("ix_auction_status_history_changed_at", "auction_status_history", ["changed_at"])

    op.create_table(
        "auction_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("city", sa.String(length=32), nullable=True),
        sa.Column("district", sa.String(length=32), nullable=True),
        sa.Column("case_type", sa.String(length=32), nullable=True),
        sa.Column("max_floor_price_twd", sa.BigInteger(), nullable=True),
        sa.Column("min_round", sa.Integer(), nullable=True),
        sa.Column("require_deliverable", sa.Boolean(), nullable=True),
        sa.Column("min_investment_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_auction_subscriptions_discord_user_id", "auction_subscriptions", ["discord_user_id"])

    # notification_logs already exists (created by 0001); add the new
    # auction link column via batch mode -- SQLite's ALTER TABLE cannot
    # add a column with an inline FOREIGN KEY constraint directly, only
    # Alembic's batch mode (copy-and-move strategy) handles that portably
    # across SQLite and PostgreSQL alike.
    with op.batch_alter_table("notification_logs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auction_case_id",
                sa.Integer(),
                sa.ForeignKey("auction_cases.id", name="fk_notification_logs_auction_case_id"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_logs") as batch_op:
        batch_op.drop_column("auction_case_id")
    for table_name in _AUCTION_TABLES_CHILD_FIRST:
        op.drop_table(table_name)
