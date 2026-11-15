"""移除 server_instances 中遗留的固定 30 天注册有效期列

Revision ID: e7f8a9b0c1d2
Revises: d4e5f6a7b8c9
Create Date: 2026-11-15 18:24:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# Alembic 版本链路标识
revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """移除 ``server_instances.registration_expiry_days`` 兼容保留列

    副作用：
        通过批处理表重建安全删除固定 30 天注册有效期字段，同时保留既有服务器配置与外键关系。
    """
    with op.batch_alter_table("server_instances", schema=None) as batch_op:
        batch_op.drop_column("registration_expiry_days")


def downgrade() -> None:
    """回滚并恢复 ``server_instances.registration_expiry_days`` 列（默认 30 天）"""
    with op.batch_alter_table("server_instances", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "registration_expiry_days",
                sa.Integer(),
                server_default=sa.text("30"),
                nullable=False,
            )
        )
