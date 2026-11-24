"""corrections: the original proposal payload, and the add_alias kind

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-08 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0021'
down_revision: str | Sequence[str] | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    'split_transaction', 'payment_match', 'capture', 'new_person',
    'follow_up', 'promise_due', 'goal_rerank', 'goal_link',
    'school_deadline', 'course_fact',
)
NEW_KINDS = (*OLD_KINDS, 'add_alias')


def upgrade() -> None:
    with op.batch_alter_table('proposal', schema=None) as batch_op:
        batch_op.add_column(sa.Column('original_json', sa.JSON(), nullable=True))
        batch_op.drop_constraint(op.f('ck_proposal_proposal_kind'), type_='check')
        batch_op.create_check_constraint('proposal_kind', "kind IN (" + ", ".join(repr(k) for k in NEW_KINDS) + ")")


def downgrade() -> None:
    op.execute("DELETE FROM proposal WHERE kind = 'add_alias'")
    with op.batch_alter_table('proposal', schema=None) as batch_op:
        batch_op.drop_constraint(op.f('ck_proposal_proposal_kind'), type_='check')
        batch_op.create_check_constraint('proposal_kind', "kind IN (" + ", ".join(repr(k) for k in OLD_KINDS) + ")")
        batch_op.drop_column('original_json')
