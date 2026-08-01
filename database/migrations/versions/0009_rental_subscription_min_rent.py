"""Add minimum monthly rent to rental subscriptions.

Revision ID: 0009_rental_min_rent
Revises: 0008_rental_keywords
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_rental_min_rent"
down_revision: str | None = "0008_rental_keywords"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rental_subscriptions",
        sa.Column("min_monthly_rent_twd", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rental_subscriptions", "min_monthly_rent_twd")
