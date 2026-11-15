"""为媒体账号新增锚定自然月周期字段及服务器有效自然月数配置

Revision ID: d4e5f6a7b8c9
Revises: c3d8e1a2b405
Create Date: 2026-11-15 17:55:00.000000

"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import context, op
from core.billing_anchor import backfill_billing_cycle

# Alembic 版本链路标识
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d8e1a2b405"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _coerce_datetime(value: datetime | str) -> datetime:
    """将数据库返回的日期字段统一转换为 datetime 对象"""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def upgrade() -> None:
    """扩展表结构并按现有到期日平滑回填存量媒体账号的周期锚定数据

    副作用：
        - 在 ``server_instances`` 表新增默认值为 ``1`` 的 ``registration_expiry_months`` 列；
        - 在 ``media_user`` 表新增 ``anchor_day``、``cycle_started_at`` 与 ``next_reset_at`` 列；
        - 在线迁移模式下批量更新缺失上述字段的存量媒体账号记录；离线 SQL 模式下生成
          ``anchor_day`` 回填语句（周期边界可在首次续期时平滑推导）。
    """
    with op.batch_alter_table("server_instances", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "registration_expiry_months",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )

    with op.batch_alter_table("media_user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("anchor_day", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cycle_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("next_reset_at", sa.DateTime(), nullable=True))

    if not context.is_offline_mode():
        # 在线迁移直接复用 Python 日期推算逻辑，精确回填存量媒体账号的 anchor_day 与周期起止边界
        conn = op.get_bind()
        media_user_table = sa.table(
            "media_user",
            sa.column("id", sa.BigInteger()),
            sa.column("server_id", sa.Integer()),
            sa.column("expires_at", sa.DateTime()),
            sa.column("anchor_day", sa.Integer()),
            sa.column("cycle_started_at", sa.DateTime()),
            sa.column("next_reset_at", sa.DateTime()),
        )
        rows = conn.execute(
            sa.select(
                media_user_table.c.id,
                media_user_table.c.server_id,
                media_user_table.c.expires_at,
                media_user_table.c.anchor_day,
                media_user_table.c.cycle_started_at,
                media_user_table.c.next_reset_at,
            ).where(
                sa.or_(
                    media_user_table.c.anchor_day.is_(None),
                    media_user_table.c.cycle_started_at.is_(None),
                    media_user_table.c.next_reset_at.is_(None),
                )
            )
        ).fetchall()

        now = datetime.now()
        for row in rows:
            anchor_day, cycle_started_at, next_reset_at = backfill_billing_cycle(
                current_expires_at=_coerce_datetime(row.expires_at),
                anchor_day=row.anchor_day,
                current_cycle_started_at=(
                    _coerce_datetime(row.cycle_started_at)
                    if row.cycle_started_at is not None
                    else None
                ),
                current_next_reset_at=(
                    _coerce_datetime(row.next_reset_at)
                    if row.next_reset_at is not None
                    else None
                ),
                now=now,
            )
            conn.execute(
                media_user_table.update()
                .where(
                    sa.and_(
                        media_user_table.c.id == row.id,
                        media_user_table.c.server_id == row.server_id,
                    )
                )
                .values(
                    anchor_day=anchor_day,
                    cycle_started_at=cycle_started_at,
                    next_reset_at=next_reset_at,
                )
            )
    else:
        # 离线 SQL 导出模式下无法读取实时行数据，输出方言兼容的 anchor_day 回填 SQL
        dialect_name = op.get_bind().dialect.name
        if dialect_name == "postgresql":
            op.execute(
                "UPDATE media_user "
                "SET anchor_day = EXTRACT(DAY FROM expires_at)::integer "
                "WHERE anchor_day IS NULL"
            )
        elif dialect_name == "sqlite":
            op.execute(
                "UPDATE media_user "
                "SET anchor_day = CAST(strftime('%d', expires_at) AS INTEGER) "
                "WHERE anchor_day IS NULL"
            )


def downgrade() -> None:
    """回滚锚定周期字段与服务器有效自然月数配置列"""
    with op.batch_alter_table("media_user", schema=None) as batch_op:
        batch_op.drop_column("next_reset_at")
        batch_op.drop_column("cycle_started_at")
        batch_op.drop_column("anchor_day")

    with op.batch_alter_table("server_instances", schema=None) as batch_op:
        batch_op.drop_column("registration_expiry_months")
