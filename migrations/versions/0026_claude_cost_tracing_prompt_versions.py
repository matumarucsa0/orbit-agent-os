"""claude call latency, cost and prompt version; proposal prompt version

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-24 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0026'
down_revision: str | Sequence[str] | None = '0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('claude_call', schema=None) as batch_op:
        batch_op.add_column(sa.Column('latency_ms', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('prompt_version', sa.String(length=60), nullable=True))
        batch_op.add_column(
            sa.Column('cost_micro_eur', sa.Integer(), nullable=False, server_default='0')
        )

    with op.batch_alter_table('proposal', schema=None) as batch_op:
        batch_op.add_column(sa.Column('prompt_version', sa.String(length=60), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('proposal', schema=None) as batch_op:
        batch_op.drop_column('prompt_version')

    with op.batch_alter_table('claude_call', schema=None) as batch_op:
        batch_op.drop_column('cost_micro_eur')
        batch_op.drop_column('prompt_version')
        batch_op.drop_column('latency_ms')
