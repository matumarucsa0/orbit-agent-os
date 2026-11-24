"""transaction search, reopening rejected proposals

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-24 17:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import orbit.domain.base
from orbit.domain.normalize import normalize_text


revision: str = '0010'
down_revision: str | Sequence[str] | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('bank_transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('message_norm', sa.Text(), nullable=True))

    with op.batch_alter_table('proposal', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reopened_at', orbit.domain.base.UTCDateTime(timezone=True), nullable=True))

    # Backfill the searchable message of rows imported before this revision.
    bind = op.get_bind()
    tx = sa.table('bank_transaction', sa.column('id', sa.Integer), sa.column('message', sa.Text),
                  sa.column('message_norm', sa.Text))
    rows = bind.execute(sa.select(tx.c.id, tx.c.message).where(tx.c.message.is_not(None))).all()
    for row_id, message in rows:
        bind.execute(tx.update().where(tx.c.id == row_id).values(message_norm=normalize_text(message)))


def downgrade() -> None:
    with op.batch_alter_table('proposal', schema=None) as batch_op:
        batch_op.drop_column('reopened_at')

    with op.batch_alter_table('bank_transaction', schema=None) as batch_op:
        batch_op.drop_column('message_norm')
