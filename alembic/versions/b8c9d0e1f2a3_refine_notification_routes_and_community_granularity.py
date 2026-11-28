"""拆分社区细粒度全局通知路由并移除媒体入库/求片全局总开关路由

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-28 12:00:00.000000

"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import context, op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPLIT_COMMUNITY_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("community_score", "main_chat"),
    ("community_verification", "admin_dm_group"),
    ("community_leave_audit", "admin_dm_group"),
)


def upgrade() -> None:
    """将旧版全局 community 路由拆分为 3 条细粒度路由，并清理废弃的全局入库/求片总开关路由。"""
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    now = datetime.now()
    routes_table = sa.table(
        "notification_routes",
        sa.column("id", sa.Integer),
        sa.column("scope_type", sa.String),
        sa.column("server_id", sa.Integer),
        sa.column("category", sa.String),
        sa.column("target_type", sa.String),
        sa.column("target_chat_id", sa.BigInteger),
        sa.column("target_topic_id", sa.BigInteger),
        sa.column("escalate_admin_dm", sa.Boolean),
        sa.column("enabled_events_json", sa.Text),
        sa.column("updated_at", sa.DateTime),
    )

    # 读取旧版全局 community 路由，升级时保留管理员已配置的投递目标以防静默丢通知
    legacy_comm = bind.execute(
        sa.select(
            routes_table.c.target_type,
            routes_table.c.target_chat_id,
            routes_table.c.target_topic_id,
            routes_table.c.escalate_admin_dm,
            routes_table.c.enabled_events_json,
        ).where(
            routes_table.c.scope_type == "global",
            routes_table.c.server_id.is_(None),
            routes_table.c.category == "community",
        )
    ).first()

    existing_global_cats = {
        row[0]
        for row in bind.execute(
            sa.select(routes_table.c.category).where(
                routes_table.c.scope_type == "global",
                routes_table.c.server_id.is_(None),
            )
        ).all()
    }

    for new_cat, default_target in _SPLIT_COMMUNITY_DEFAULTS:
        if new_cat in existing_global_cats:
            continue
        if legacy_comm is not None:
            bind.execute(
                routes_table.insert().values(
                    scope_type="global",
                    server_id=None,
                    category=new_cat,
                    target_type=legacy_comm[0],
                    target_chat_id=legacy_comm[1],
                    target_topic_id=legacy_comm[2],
                    escalate_admin_dm=bool(legacy_comm[3]),
                    enabled_events_json=legacy_comm[4],
                    updated_at=now,
                )
            )
        else:
            bind.execute(
                routes_table.insert().values(
                    scope_type="global",
                    server_id=None,
                    category=new_cat,
                    target_type=default_target,
                    target_chat_id=None,
                    target_topic_id=None,
                    escalate_admin_dm=False,
                    enabled_events_json=None,
                    updated_at=now,
                )
            )

    # 清理废弃的全局 community、media_library、media_request 行（单服行完整保留）
    bind.execute(
        routes_table.delete().where(
            routes_table.c.scope_type == "global",
            routes_table.c.server_id.is_(None),
            routes_table.c.category.in_(
                ("community", "media_library", "media_request")
            ),
        )
    )


def downgrade() -> None:
    """回滚合并 3 条社区细粒度路由为旧版 community，并恢复全局 media_library/media_request 禁用行。"""
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    now = datetime.now()
    routes_table = sa.table(
        "notification_routes",
        sa.column("id", sa.Integer),
        sa.column("scope_type", sa.String),
        sa.column("server_id", sa.Integer),
        sa.column("category", sa.String),
        sa.column("target_type", sa.String),
        sa.column("target_chat_id", sa.BigInteger),
        sa.column("target_topic_id", sa.BigInteger),
        sa.column("escalate_admin_dm", sa.Boolean),
        sa.column("enabled_events_json", sa.Text),
        sa.column("updated_at", sa.DateTime),
    )

    existing_global_cats = {
        row[0]
        for row in bind.execute(
            sa.select(routes_table.c.category).where(
                routes_table.c.scope_type == "global",
                routes_table.c.server_id.is_(None),
            )
        ).all()
    }

    score_row = bind.execute(
        sa.select(
            routes_table.c.target_type,
            routes_table.c.target_chat_id,
            routes_table.c.target_topic_id,
            routes_table.c.escalate_admin_dm,
            routes_table.c.enabled_events_json,
        ).where(
            routes_table.c.scope_type == "global",
            routes_table.c.server_id.is_(None),
            routes_table.c.category == "community_score",
        )
    ).first()

    if "community" not in existing_global_cats:
        bind.execute(
            routes_table.insert().values(
                scope_type="global",
                server_id=None,
                category="community",
                target_type=score_row[0] if score_row is not None else "main_chat",
                target_chat_id=score_row[1] if score_row is not None else None,
                target_topic_id=score_row[2] if score_row is not None else None,
                escalate_admin_dm=bool(score_row[3])
                if score_row is not None
                else False,
                enabled_events_json=score_row[4] if score_row is not None else None,
                updated_at=now,
            )
        )

    for legacy_cat in ("media_library", "media_request"):
        if legacy_cat not in existing_global_cats:
            bind.execute(
                routes_table.insert().values(
                    scope_type="global",
                    server_id=None,
                    category=legacy_cat,
                    target_type="disabled",
                    target_chat_id=None,
                    target_topic_id=None,
                    escalate_admin_dm=False,
                    enabled_events_json=None,
                    updated_at=now,
                )
            )

    bind.execute(
        routes_table.delete().where(
            routes_table.c.scope_type == "global",
            routes_table.c.server_id.is_(None),
            routes_table.c.category.in_(
                ("community_score", "community_verification", "community_leave_audit")
            ),
        )
    )
