"""add allow_nfo to server_instances

Revision ID: 9a4c2e1f8b10
Revises: 7c2e4a9f1d03
Create Date: 2026-11-13 15:43:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a4c2e1f8b10'
down_revision: Union[str, Sequence[str], None] = '7c2e4a9f1d03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('server_instances', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('allow_nfo', sa.Boolean(), server_default=sa.text('true'), nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('server_instances', schema=None) as batch_op:
        batch_op.drop_column('allow_nfo')
