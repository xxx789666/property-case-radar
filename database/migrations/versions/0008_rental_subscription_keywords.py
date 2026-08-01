"""Add OR-keyword matching to rental subscriptions.

Revision ID: 0008_rental_keywords
Revises: 0007_rental_subscriptions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_rental_keywords"
down_revision: str | None = "0007_rental_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rental_subscriptions",
        sa.Column("keywords_any", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rental_subscriptions", "keywords_any")
