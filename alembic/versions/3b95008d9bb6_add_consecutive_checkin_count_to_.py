"""add consecutive_checkin_count to telegram_users

Revision ID: 3b95008d9bb6
Revises: ed544a07e347
Create Date: 2026-09-24 15:09:47.789852

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b95008d9bb6'
down_revision: Union[str, Sequence[str], None] = 'ed544a07e347'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('telegram_users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('consecutive_checkin_count', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('telegram_users', schema=None) as batch_op:
        batch_op.drop_column('consecutive_checkin_count')
