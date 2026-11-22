"""新增统一通知路由、兜底收件箱、运维告警与用户偏好表及追踪列，并回填存量服务器话题路由

Revision ID: f2a3b4c5d6e7
Revises: e6f7a8b9c0d1
Create Date: 2026-11-21 22:56:00.000000

"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import context, op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_GLOBAL_ROUTES: tuple[tuple[str, str], ...] = (
    ("media_library", "disabled"),
    ("media_request", "disabled"),
    ("account_lifecycle", "disabled"),
    ("ops_alert", "admin_dm_group"),
    ("community", "main_chat"),
)


def _classify_legacy_topic(
    topic_val: int | None,
) -> tuple[str, int | None, int | None] | None:
    """将旧版 ServerInstance 话题字段映射为分层路由目标参数

    Args:
        topic_val: 旧字段数值（正数表示论坛 Topic ID，负数表示独立群组/频道 Chat ID，0 或 None 表示未设置）。

    Returns:
        (target_type, target_chat_id, target_topic_id) 元组；未配置时返回 ``None``。
    """
    if topic_val is None or topic_val == 0:
        return None
    if topic_val > 0:
        return ("forum_topic", None, int(topic_val))
    return ("chat_or_channel", int(topic_val), None)


def upgrade() -> None:
    """创建通知管线 4 张新表、补充生命周期/求片追踪列，并初始化默认路由与服务器覆盖回填"""
    op.create_table(
        "notification_routes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column(
            "target_type",
            sa.String(length=32),
            server_default=sa.text("'disabled'"),
            nullable=False,
        ),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("target_topic_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "escalate_admin_dm",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("enabled_events_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server_instances.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_routes_server_id",
        "notification_routes",
        ["server_id"],
        unique=False,
    )
    op.create_index(
        "uq_notification_route_global_cat",
        "notification_routes",
        ["scope_type", "category"],
        unique=True,
        sqlite_where=sa.text("server_id IS NULL"),
        postgresql_where=sa.text("server_id IS NULL"),
    )
    op.create_index(
        "uq_notification_route_server_cat",
        "notification_routes",
        ["scope_type", "server_id", "category"],
        unique=True,
        sqlite_where=sa.text("server_id IS NOT NULL"),
        postgresql_where=sa.text("server_id IS NOT NULL"),
    )

    op.create_table(
        "notification_inbox_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column(
            "severity",
            sa.String(length=16),
            server_default=sa.text("'info'"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("action_url", sa.String(length=512), nullable=True),
        sa.Column(
            "is_mandatory",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_read",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("delivery_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server_instances.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["telegram_users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_inbox_items_user_id",
        "notification_inbox_items",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_inbox_items_is_read",
        "notification_inbox_items",
        ["is_read"],
        unique=False,
    )
    op.create_index(
        "ix_notification_inbox_items_created_at",
        "notification_inbox_items",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "ops_alert_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "severity",
            sa.String(length=16),
            server_default=sa.text("'warning'"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column(
            "delivered_targets_json",
            sa.Text(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "delivery_status",
            sa.String(length=32),
            server_default=sa.text("'sent'"),
            nullable=False,
        ),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server_instances.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ops_alert_logs_source_key",
        "ops_alert_logs",
        ["source_key"],
        unique=False,
    )
    op.create_index(
        "ix_ops_alert_logs_created_at",
        "ops_alert_logs",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "user_notification_preferences",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "allow_watch_time_milestone",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_pre_expiry_reminder",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_traffic_80_alert",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_cycle_reset",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_self_action_receipt",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["telegram_users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    # 合并下游 Ticket 05/06 所需的追踪字段，避免形成多条并行迁移分支
    with op.batch_alter_table("media_requests") as batch_op:
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "watch_time_milestone_notified",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "grace_delete_reminder_sent",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )

    if not context.is_offline_mode():
        conn = op.get_bind()
        now = datetime.now()
        routes_table = sa.table(
            "notification_routes",
            sa.column("scope_type", sa.String(16)),
            sa.column("server_id", sa.Integer()),
            sa.column("category", sa.String(32)),
            sa.column("target_type", sa.String(32)),
            sa.column("target_chat_id", sa.BigInteger()),
            sa.column("target_topic_id", sa.BigInteger()),
            sa.column("escalate_admin_dm", sa.Boolean()),
            sa.column("enabled_events_json", sa.Text()),
            sa.column("updated_at", sa.DateTime()),
        )

        # 1. 初始化 5 条全局默认路由（兼容旧库静默习惯，仅社区主群与运维管理组默认开）
        seed_rows: list[dict[str, object]] = [
            {
                "scope_type": "global",
                "server_id": None,
                "category": category,
                "target_type": target_type,
                "target_chat_id": None,
                "target_topic_id": None,
                "escalate_admin_dm": False,
                "enabled_events_json": None,
                "updated_at": now,
            }
            for category, target_type in _DEFAULT_GLOBAL_ROUTES
        ]

        # 2. 将存量服务器的 notify_topic_id / request_notify_topic_id 平滑回填为单服覆盖路由
        srv_table = sa.table(
            "server_instances",
            sa.column("id", sa.Integer()),
            sa.column("notify_topic_id", sa.BigInteger()),
            sa.column("request_notify_topic_id", sa.BigInteger()),
        )
        server_rows = conn.execute(
            sa.select(
                srv_table.c.id,
                srv_table.c.notify_topic_id,
                srv_table.c.request_notify_topic_id,
            )
        ).fetchall()
        for server_id, notify_topic_id, request_notify_topic_id in server_rows:
            media_target = _classify_legacy_topic(notify_topic_id)
            if media_target is not None:
                target_type, target_chat_id, target_topic_id = media_target
                seed_rows.append(
                    {
                        "scope_type": "server",
                        "server_id": server_id,
                        "category": "media_library",
                        "target_type": target_type,
                        "target_chat_id": target_chat_id,
                        "target_topic_id": target_topic_id,
                        "escalate_admin_dm": False,
                        "enabled_events_json": None,
                        "updated_at": now,
                    }
                )
            req_target = _classify_legacy_topic(request_notify_topic_id)
            if req_target is not None:
                target_type, target_chat_id, target_topic_id = req_target
                seed_rows.append(
                    {
                        "scope_type": "server",
                        "server_id": server_id,
                        "category": "media_request",
                        "target_type": target_type,
                        "target_chat_id": target_chat_id,
                        "target_topic_id": target_topic_id,
                        "escalate_admin_dm": False,
                        "enabled_events_json": None,
                        "updated_at": now,
                    }
                )

        if seed_rows:
            conn.execute(sa.insert(routes_table), seed_rows)


def downgrade() -> None:
    """回滚追踪列与通知领域 4 张新表（保留既有 ServerInstance 旧话题列）"""
    with op.batch_alter_table("media_user") as batch_op:
        batch_op.drop_column("grace_delete_reminder_sent")
        batch_op.drop_column("watch_time_milestone_notified")

    with op.batch_alter_table("media_requests") as batch_op:
        batch_op.drop_column("completed_at")

    op.drop_table("user_notification_preferences")

    op.drop_index("ix_ops_alert_logs_created_at", table_name="ops_alert_logs")
    op.drop_index("ix_ops_alert_logs_source_key", table_name="ops_alert_logs")
    op.drop_table("ops_alert_logs")

    op.drop_index(
        "ix_notification_inbox_items_created_at",
        table_name="notification_inbox_items",
    )
    op.drop_index(
        "ix_notification_inbox_items_is_read",
        table_name="notification_inbox_items",
    )
    op.drop_index(
        "ix_notification_inbox_items_user_id",
        table_name="notification_inbox_items",
    )
    op.drop_table("notification_inbox_items")

    op.drop_index("uq_notification_route_server_cat", table_name="notification_routes")
    op.drop_index("uq_notification_route_global_cat", table_name="notification_routes")
    op.drop_index("ix_notification_routes_server_id", table_name="notification_routes")
    op.drop_table("notification_routes")
