"""新增服务器时长保活开关/门槛与媒体账号自动保活字段

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-11-16 16:42:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展服务器时长保活策略字段及媒体账号默认关闭的自动保活开关"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enable_watch_time_renewal",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "watch_time_renewal_threshold_seconds",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_renew",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )


def downgrade() -> None:
    """回滚服务器时长保活配置与媒体账号自动保活字段"""
    with op.batch_alter_table("media_user") as batch_op:
        batch_op.drop_column("auto_renew")

    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("watch_time_renewal_threshold_seconds")
        batch_op.drop_column("enable_watch_time_renewal")
