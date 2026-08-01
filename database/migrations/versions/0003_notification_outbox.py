"""notification outbox: pending/delivered/failed tracking with dedupe

Revision ID: 0003
Revises: 0002

Deliberately does NOT import database.models / call
Base.metadata.create_all() -- see revision 0001's module docstring for
why. Redefines ``notification_logs`` as a proper durable outbox table
(``status``, ``delivery_key`` unique dedupe key, ``attempt_count``/
``last_error`` bounded-retry bookkeeping, ``created_at``/
``delivered_at``, and ``status_history_id`` so a status-change
notification can be re-rendered from DB state alone on a later delivery
attempt) instead of the plain send-log shape from 0002.

``notification_logs`` has had zero writers anywhere in this codebase
before this revision (queuing/delivery land together with it), so this
is implemented as a clean drop-and-recreate rather than a column-by-
column ALTER sequence -- there is no production data anywhere to
preserve or migrate for this table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("notification_logs")
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("property_id", sa.Integer(), sa.ForeignKey("properties.id"), nullable=True),
        sa.Column("auction_case_id", sa.Integer(), sa.ForeignKey("auction_cases.id"), nullable=True),
        sa.Column(
            "status_history_id", sa.Integer(), sa.ForeignKey("auction_status_history.id"), nullable=True
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("delivery_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("delivery_key", name="uq_notification_logs_delivery_key"),
    )
    op.create_index("ix_notification_logs_status", "notification_logs", ["status"])
    op.create_index("ix_notification_logs_auction_case_id", "notification_logs", ["auction_case_id"])


def downgrade() -> None:
    op.drop_table("notification_logs")
    # Restore the 0002-era plain send-log shape exactly.
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("property_id", sa.Integer(), sa.ForeignKey("properties.id"), nullable=True),
        sa.Column("auction_case_id", sa.Integer(), sa.ForeignKey("auction_cases.id"), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
