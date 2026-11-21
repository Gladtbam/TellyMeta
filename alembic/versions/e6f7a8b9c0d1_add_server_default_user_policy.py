"""为服务器实例新增默认 UserPolicy JSON 模板配置列

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-21 12:58:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """在 server_instances 表新增可空的 default_user_policy 文本列"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.add_column(sa.Column("default_user_policy", sa.Text(), nullable=True))


def downgrade() -> None:
    """移除 server_instances 表中的 default_user_policy 列"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("default_user_policy")
