"""auction tables + notification_logs auction link

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

from database.models import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Child-before-parent order, for a clean downgrade.
_AUCTION_TABLES = (
    "auction_status_history",
    "auction_documents",
    "auction_rounds",
    "auction_subscriptions",
    "auction_cases",
)


def upgrade() -> None:
    bind = op.get_bind()
    # create_all() is idempotent (checkfirst=True) and, since
    # database.models.auction is now imported into Base.metadata, creates
    # exactly the tables that don't exist yet. On a fresh install (0001
    # then 0002 run back-to-back against current code) this is a no-op --
    # 0001's own create_all already created everything. On a database that
    # ran 0001 before the auction vertical existed, this adds the five new
    # auction_* tables.
    Base.metadata.create_all(bind=bind)

    # create_all() cannot ALTER an existing table: notification_logs was
    # already created by 0001 on any pre-existing deployment, so the new
    # auction_case_id link column must be added explicitly. Guarded with
    # an inspect() check so this migration is itself idempotent (a fresh
    # install's notification_logs, created by the create_all() call
    # above, already has the column).
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("notification_logs")}
    if "auction_case_id" not in existing_columns:
        # batch_alter_table (copy-and-move strategy): SQLite's ALTER TABLE
        # cannot add a column with an inline FOREIGN KEY constraint
        # directly -- only Alembic's batch mode handles that portably
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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("notification_logs")}
    if "auction_case_id" in existing_columns:
        with op.batch_alter_table("notification_logs") as batch_op:
            batch_op.drop_column("auction_case_id")
    for table_name in _AUCTION_TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
