"""create media_requests table

Revision ID: 7c2e4a9f1d03
Revises: 3b95008d9bb6
Create Date: 2026-11-13 14:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c2e4a9f1d03'
down_revision: Union[str, Sequence[str], None] = '3b95008d9bb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'media_requests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('media_server_id', sa.Integer(), nullable=False),
        sa.Column('arr_id', sa.Integer(), nullable=False),
        sa.Column('library_name', sa.String(length=128), nullable=False),
        sa.Column('media_type', sa.String(length=16), nullable=False),
        sa.Column('media_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('reserved_cost', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('cost_status', sa.String(length=32), nullable=False),
        sa.Column('topic_message_id', sa.BigInteger(), nullable=True),
        sa.Column('approver_name', sa.String(length=128), nullable=True),
        sa.Column('fail_reason', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['arr_id'], ['server_instances.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['media_server_id'], ['server_instances.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['telegram_users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_media_requests_arr_id', 'media_requests', ['arr_id'], unique=False)
    op.create_index('ix_media_requests_expires_at', 'media_requests', ['expires_at'], unique=False)
    op.create_index('ix_media_requests_status', 'media_requests', ['status'], unique=False)
    op.create_index('ix_media_requests_user_id', 'media_requests', ['user_id'], unique=False)
    op.create_index(
        'uq_pending_arr_media',
        'media_requests',
        ['arr_id', 'media_type', 'media_id'],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_pending_arr_media', table_name='media_requests')
    op.drop_index('ix_media_requests_user_id', table_name='media_requests')
    op.drop_index('ix_media_requests_status', table_name='media_requests')
    op.drop_index('ix_media_requests_expires_at', table_name='media_requests')
    op.drop_index('ix_media_requests_arr_id', table_name='media_requests')
    op.drop_table('media_requests')
