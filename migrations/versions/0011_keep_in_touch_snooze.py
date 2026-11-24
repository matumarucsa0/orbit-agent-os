"""keep in touch snooze

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30 18:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0011'
down_revision: str | Sequence[str] | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.add_column(sa.Column('snoozed_until', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.drop_column('snoozed_until')
