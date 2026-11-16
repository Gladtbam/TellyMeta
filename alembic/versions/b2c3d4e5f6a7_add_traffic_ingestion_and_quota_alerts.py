"""新增服务器流量采集/告警/配额字段与媒体账号流量累计和告警去重字段

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-11-16 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为服务器实例与媒体账号扩展流量采集配置、周期累计消费及告警幂等标志列"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enable_traffic",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "enable_traffic_alert",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_quota_bytes",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cycle_traffic_bytes",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "total_traffic_bytes",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_alert_80_sent",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_alert_100_sent",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )


def downgrade() -> None:
    """回滚服务器与媒体账号的流量采集及告警去重列"""
    with op.batch_alter_table("media_user") as batch_op:
        batch_op.drop_column("traffic_alert_100_sent")
        batch_op.drop_column("traffic_alert_80_sent")
        batch_op.drop_column("total_traffic_bytes")
        batch_op.drop_column("cycle_traffic_bytes")

    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("traffic_quota_bytes")
        batch_op.drop_column("enable_traffic_alert")
        batch_op.drop_column("enable_traffic")
