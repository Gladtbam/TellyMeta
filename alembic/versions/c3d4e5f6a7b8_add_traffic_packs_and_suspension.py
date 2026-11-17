"""新增服务器超流自动停用开关与媒体账号流量包余额及超流停用隔离标记

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-17 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为服务器实例新增超流自动停用开关，为媒体账号新增跨周期流量包余额与独立超流停用标记"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enable_traffic_auto_suspend",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "traffic_pack_bytes",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_traffic_suspended",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )


def downgrade() -> None:
    """回滚服务器超流自动停用开关及媒体账号流量包余额与超流停用标记列"""
    with op.batch_alter_table("media_user") as batch_op:
        batch_op.drop_column("is_traffic_suspended")
        batch_op.drop_column("traffic_pack_bytes")

    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("enable_traffic_auto_suspend")
