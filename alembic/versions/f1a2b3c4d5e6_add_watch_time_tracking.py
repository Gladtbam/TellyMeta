"""新增有效播放时长统计表结构与服务器/账号采集字段

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-11-15 20:53:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展服务器与媒体账号有效播放时长字段，并新增日统计、时间线区间与会话快照表"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enable_watch_time",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            )
        )

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cycle_watch_time_seconds",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "total_watch_time_seconds",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "watch_time_remainder_ms",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    op.create_table(
        "media_watch_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("watch_date", sa.Date(), nullable=False),
        sa.Column(
            "watch_seconds",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "server_id"],
            ["media_user.id", "media_user.server_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "server_id",
            "watch_date",
            name="uq_media_watch_daily_account_date",
        ),
    )
    op.create_index(
        "ix_media_watch_daily_user_id", "media_watch_daily", ["user_id"], unique=False
    )
    op.create_index(
        "ix_media_watch_daily_server_id",
        "media_watch_daily",
        ["server_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_watch_daily_watch_date",
        "media_watch_daily",
        ["watch_date"],
        unique=False,
    )

    op.create_table(
        "media_watch_intervals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "server_id"],
            ["media_user.id", "media_user.server_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_watch_intervals_user_id",
        "media_watch_intervals",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_watch_intervals_server_id",
        "media_watch_intervals",
        ["server_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_watch_intervals_end_at",
        "media_watch_intervals",
        ["end_at"],
        unique=False,
    )

    op.create_table(
        "media_playback_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("play_session_id", sa.String(length=128), nullable=True),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "is_paused",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("last_position_ticks", sa.BigInteger(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=False),
        sa.Column("last_effective_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id", "server_id"],
            ["media_user.id", "media_user.server_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_id",
            "session_key",
            name="uq_media_playback_session_server_key",
        ),
    )
    op.create_index(
        "ix_media_playback_sessions_user_id",
        "media_playback_sessions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_playback_sessions_server_id",
        "media_playback_sessions",
        ["server_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_playback_sessions_is_active",
        "media_playback_sessions",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    """回滚播放时长统计新增表及扩展列"""
    op.drop_index(
        "ix_media_playback_sessions_is_active", table_name="media_playback_sessions"
    )
    op.drop_index(
        "ix_media_playback_sessions_server_id", table_name="media_playback_sessions"
    )
    op.drop_index(
        "ix_media_playback_sessions_user_id", table_name="media_playback_sessions"
    )
    op.drop_table("media_playback_sessions")

    op.drop_index(
        "ix_media_watch_intervals_end_at", table_name="media_watch_intervals"
    )
    op.drop_index(
        "ix_media_watch_intervals_server_id", table_name="media_watch_intervals"
    )
    op.drop_index(
        "ix_media_watch_intervals_user_id", table_name="media_watch_intervals"
    )
    op.drop_table("media_watch_intervals")

    op.drop_index("ix_media_watch_daily_watch_date", table_name="media_watch_daily")
    op.drop_index("ix_media_watch_daily_server_id", table_name="media_watch_daily")
    op.drop_index("ix_media_watch_daily_user_id", table_name="media_watch_daily")
    op.drop_table("media_watch_daily")

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.drop_column("watch_time_remainder_ms")
        batch_op.drop_column("total_watch_time_seconds")
        batch_op.drop_column("cycle_watch_time_seconds")

    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("enable_watch_time")
