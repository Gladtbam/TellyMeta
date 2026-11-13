"""add qbittorrent support to server_instances

Revision ID: c3d8e1a2b405
Revises: 9a4c2e1f8b10
Create Date: 2026-11-13 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d8e1a2b405'
down_revision: Union[str, Sequence[str], None] = '9a4c2e1f8b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('server_instances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('qb_username', sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column('qb_torrent_limit', sa.Integer(), server_default=sa.text('43200'), nullable=False)
        )
        batch_op.add_column(sa.Column('qb_server_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_server_instances_qb_server_id',
            'server_instances',
            ['qb_server_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('server_instances', schema=None) as batch_op:
        batch_op.drop_constraint('fk_server_instances_qb_server_id', type_='foreignkey')
        batch_op.drop_column('qb_server_id')
        batch_op.drop_column('qb_torrent_limit')
        batch_op.drop_column('qb_username')
