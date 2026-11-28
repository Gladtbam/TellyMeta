"""将全库时间列统一迁移为 UTC（PostgreSQL TIMESTAMPTZ / SQLite 规范化 UTC）

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-28 13:40:00.000000

"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from alembic import op
from core.config import get_settings

# 修订标识，供 Alembic 链式迁移使用
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 全库 12 张表共 25 个时间列清单：(表名, 主键列元组, 时间列元组)
DATETIME_TABLE_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("telegram_users", ("id",), ("last_checkin",)),
    (
        "media_user",
        ("id", "server_id"),
        ("expires_at", "cycle_started_at", "next_reset_at", "delete_at"),
    ),
    ("active_codes", ("id",), ("created_at", "used_at", "expires_at")),
    ("api_cache", ("key",), ("expires_at", "created_at")),
    (
        "media_requests",
        ("id",),
        ("created_at", "expires_at", "resolved_at", "completed_at"),
    ),
    ("media_watch_daily", ("id",), ("updated_at",)),
    ("media_watch_intervals", ("id",), ("start_at", "end_at")),
    (
        "media_playback_sessions",
        ("id",),
        ("last_event_at", "last_effective_at", "ended_at"),
    ),
    ("notification_routes", ("id",), ("updated_at",)),
    ("notification_inbox_items", ("id",), ("created_at", "read_at")),
    ("ops_alert_logs", ("id",), ("created_at",)),
    ("user_notification_preferences", ("user_id",), ("updated_at",)),
)


def _resolve_source_timezone() -> tuple[str, ZoneInfo]:
    """解析历史朴素时间所属的源时区（优先读取 MIGRATION_SOURCE_TIMEZONE 环境变量）。

    Returns:
        ``(IANA 时区名, ZoneInfo 对象)`` 二元组；非法时区名会立即抛出 ``ZoneInfoNotFoundError``。
    """
    tz_name = (
        os.environ.get("MIGRATION_SOURCE_TIMEZONE")
        or get_settings().timezone
        or "Asia/Shanghai"
    ).strip() or "Asia/Shanghai"
    return tz_name, ZoneInfo(tz_name)


def _convert_sqlite_value(
    raw_value: object,
    *,
    source_tz: ZoneInfo,
    to_utc: bool,
) -> str | None:
    """将 SQLite 中存储的时间文本在源时区与 UTC 之间互转，保持 SQLAlchemy 标准格式。"""
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        parsed = raw_value
    else:
        text_val = str(raw_value).strip()
        if not text_val:
            return None
        parsed = datetime.fromisoformat(text_val)

    if to_utc:
        aware = (
            parsed.astimezone(UTC)
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=source_tz).astimezone(UTC)
        )
        return aware.replace(tzinfo=None).isoformat(sep=" ")

    aware = (
        parsed.astimezone(source_tz)
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=UTC).astimezone(source_tz)
    )
    return aware.replace(tzinfo=None).isoformat(sep=" ")


def _migrate_postgresql_columns(*, to_timestamptz: bool) -> None:
    """在 PostgreSQL 上原地将全部时间列切换为 TIMESTAMPTZ（或降级回 TIMESTAMP）。"""
    bind = op.get_bind()
    tz_name, _ = _resolve_source_timezone()
    safe_tz_literal = tz_name.replace("'", "''")
    expected_current_type = (
        "timestamp without time zone" if to_timestamptz else "timestamp with time zone"
    )
    target_sql_type = "TIMESTAMPTZ" if to_timestamptz else "TIMESTAMP WITHOUT TIME ZONE"

    for table_name, _pk_cols, dt_cols in DATETIME_TABLE_SPECS:
        for col_name in dt_cols:
            # 查询当前列实际数据类型，确保重复执行或部分预迁移库下的幂等性
            current_type = bind.execute(
                sa.text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table_name AND column_name = :col_name"
                ),
                {"table_name": table_name, "col_name": col_name},
            ).scalar_one_or_none()
            if current_type != expected_current_type:
                continue
            bind.execute(
                sa.text(
                    f'ALTER TABLE "{table_name}" '
                    f'ALTER COLUMN "{col_name}" TYPE {target_sql_type} '
                    f"USING \"{col_name}\" AT TIME ZONE '{safe_tz_literal}'"
                )
            )


def _migrate_sqlite_values(*, to_utc: bool) -> None:
    """在 SQLite 上将全部时间列存量值按源时区与 UTC 互转（表结构保持 DATETIME）。"""
    bind = op.get_bind()
    _, source_tz = _resolve_source_timezone()

    for table_name, pk_cols, dt_cols in DATETIME_TABLE_SPECS:
        select_cols = ", ".join(f'"{c}"' for c in (*pk_cols, *dt_cols))
        rows = (
            bind.execute(sa.text(f'SELECT {select_cols} FROM "{table_name}"'))
            .mappings()
            .all()
        )
        if not rows:
            continue

        set_clause = ", ".join(f'"{c}" = :new_{c}' for c in dt_cols)
        where_clause = " AND ".join(f'"{pk}" = :pk_{pk}' for pk in pk_cols)
        update_stmt = sa.text(
            f'UPDATE "{table_name}" SET {set_clause} WHERE {where_clause}'
        )
        for row in rows:
            params: dict[str, object] = {f"pk_{pk}": row[pk] for pk in pk_cols}
            changed = False
            for col in dt_cols:
                raw_val = row[col]
                converted = _convert_sqlite_value(
                    raw_val, source_tz=source_tz, to_utc=to_utc
                )
                params[f"new_{col}"] = converted
                if converted != raw_val:
                    changed = True
            if changed:
                bind.execute(update_stmt, params)


def upgrade() -> None:
    """将全部无时区本地时间列按配置源时区转换为 UTC（PG 为 TIMESTAMPTZ，SQLite 为规范 UTC）。"""
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        _migrate_postgresql_columns(to_timestamptz=True)
    elif dialect_name == "sqlite":
        _migrate_sqlite_values(to_utc=True)


def downgrade() -> None:
    """将全库 UTC 时间列可逆还原为配置源时区的无时区时间。"""
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        _migrate_postgresql_columns(to_timestamptz=False)
    elif dialect_name == "sqlite":
        _migrate_sqlite_values(to_utc=False)
