"""为服务器新增独立求片/流量包开关与定价及媒体账号当期临时调额列，并迁移清理旧全局定价

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-11-19 21:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEPRECATED_GLOBAL_KEYS = (
    "points:request_mode",
    "points:request_ratio",
    "points:request_fixed",
    "traffic:pack_cost_score",
    "traffic:pack_size_gib",
)


def _parse_int(raw: str | None, default: int, minimum: int) -> int:
    """安全解析整数配置并应用下界约束"""
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return default


def _parse_float(raw: str | None, default: float, minimum: float) -> float:
    """安全解析浮点配置并应用下界约束"""
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def upgrade() -> None:
    """扩展服务器独立求片与流量包定价字段及当期调额字段，并完成存量回填与旧全局键清理"""
    legacy_values: dict[str, str] = {}
    if not context.is_offline_mode():
        conn = op.get_bind()
        cfg_table = sa.table(
            "bot_configurations",
            sa.column("key", sa.String(64)),
            sa.column("value", sa.String(255)),
        )
        rows = conn.execute(
            sa.select(cfg_table.c.key, cfg_table.c.value).where(
                cfg_table.c.key.in_(_DEPRECATED_GLOBAL_KEYS)
            )
        ).fetchall()
        legacy_values = {row[0]: row[1] for row in rows}

    with op.batch_alter_table("server_instances") as batch_op:
        # 新建服务器默认关闭求片，存量服务器行保留各自原值不被覆盖
        batch_op.alter_column(
            "allow_request",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            existing_server_default=sa.text("(true)"),
        )
        batch_op.add_column(
            sa.Column(
                "request_points_mode",
                sa.String(16),
                server_default=sa.text("'fixed'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "request_points_fixed",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "request_points_ratio",
                sa.Float(),
                server_default=sa.text("0.0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "allow_traffic_pack",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_pack_points_mode",
                sa.String(16),
                server_default=sa.text("'fixed'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_pack_points_fixed",
                sa.Integer(),
                server_default=sa.text("50"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_pack_points_ratio",
                sa.Float(),
                server_default=sa.text("0.5"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "traffic_pack_size_gib",
                sa.Float(),
                server_default=sa.text("10.0"),
                nullable=False,
            )
        )

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cycle_traffic_adjustment_bytes",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    if not context.is_offline_mode():
        conn = op.get_bind()
        req_mode = (legacy_values.get("points:request_mode") or "ratio").strip()
        if req_mode not in ("fixed", "ratio"):
            req_mode = "ratio"
        req_ratio = _parse_float(legacy_values.get("points:request_ratio"), 0.1, 0.0)
        req_fixed = _parse_int(legacy_values.get("points:request_fixed"), 10, 0)
        pack_fixed = _parse_int(legacy_values.get("traffic:pack_cost_score"), 50, 1)
        pack_size_gib = _parse_float(
            legacy_values.get("traffic:pack_size_gib"), 10.0, 0.0
        )
        if pack_size_gib <= 0 or round(pack_size_gib * (1024**3)) < 1:
            pack_size_gib = 10.0

        srv_table = sa.table(
            "server_instances",
            sa.column("enable_traffic", sa.Boolean()),
            sa.column("traffic_quota_bytes", sa.BigInteger()),
            sa.column("request_points_mode", sa.String(16)),
            sa.column("request_points_fixed", sa.Integer()),
            sa.column("request_points_ratio", sa.Float()),
            sa.column("allow_traffic_pack", sa.Boolean()),
            sa.column("traffic_pack_points_mode", sa.String(16)),
            sa.column("traffic_pack_points_fixed", sa.Integer()),
            sa.column("traffic_pack_points_ratio", sa.Float()),
            sa.column("traffic_pack_size_gib", sa.Float()),
        )
        # 存量服务器继承旧全局求片定价与流量包单价；仅在已开启流量且配额 > 0 时启用流量包
        conn.execute(
            sa.update(srv_table).values(
                request_points_mode=req_mode,
                request_points_fixed=req_fixed,
                request_points_ratio=req_ratio,
                allow_traffic_pack=sa.case(
                    (
                        sa.and_(
                            srv_table.c.enable_traffic.is_(True),
                            srv_table.c.traffic_quota_bytes > 0,
                        ),
                        True,
                    ),
                    else_=False,
                ),
                traffic_pack_points_mode="fixed",
                traffic_pack_points_fixed=pack_fixed,
                traffic_pack_points_ratio=0.5,
                traffic_pack_size_gib=pack_size_gib,
            )
        )

        cfg_table = sa.table(
            "bot_configurations",
            sa.column("key", sa.String(64)),
        )
        conn.execute(
            sa.delete(cfg_table).where(cfg_table.c.key.in_(_DEPRECATED_GLOBAL_KEYS))
        )


def downgrade() -> None:
    """回滚当期调额与服务器独立定价列，并从存量服务器反向恢复旧全局定价键"""
    if not context.is_offline_mode():
        conn = op.get_bind()
        srv_table = sa.table(
            "server_instances",
            sa.column("server_type", sa.String(32)),
            sa.column("request_points_mode", sa.String(16)),
            sa.column("request_points_fixed", sa.Integer()),
            sa.column("request_points_ratio", sa.Float()),
            sa.column("traffic_pack_points_fixed", sa.Integer()),
            sa.column("traffic_pack_size_gib", sa.Float()),
        )
        sample = conn.execute(
            sa.select(
                srv_table.c.request_points_mode,
                srv_table.c.request_points_ratio,
                srv_table.c.request_points_fixed,
                srv_table.c.traffic_pack_points_fixed,
                srv_table.c.traffic_pack_size_gib,
            ).order_by(
                sa.case(
                    (srv_table.c.server_type.in_(("emby", "jellyfin")), 0),
                    else_=1,
                )
            )
        ).first()
        restored = {
            "points:request_mode": str(sample[0]) if sample else "ratio",
            "points:request_ratio": str(sample[1]) if sample else "0.1",
            "points:request_fixed": str(sample[2]) if sample else "10",
            "traffic:pack_cost_score": str(sample[3]) if sample else "50",
            "traffic:pack_size_gib": str(sample[4]) if sample else "10.0",
        }
        cfg_table = sa.table(
            "bot_configurations",
            sa.column("key", sa.String(64)),
            sa.column("value", sa.String(255)),
        )
        existing_keys = {
            row[0]
            for row in conn.execute(
                sa.select(cfg_table.c.key).where(
                    cfg_table.c.key.in_(_DEPRECATED_GLOBAL_KEYS)
                )
            ).fetchall()
        }
        to_insert = [
            {"key": k, "value": v}
            for k, v in restored.items()
            if k not in existing_keys
        ]
        if to_insert:
            conn.execute(sa.insert(cfg_table), to_insert)

    with op.batch_alter_table("media_user") as batch_op:
        batch_op.drop_column("cycle_traffic_adjustment_bytes")

    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("traffic_pack_size_gib")
        batch_op.drop_column("traffic_pack_points_ratio")
        batch_op.drop_column("traffic_pack_points_fixed")
        batch_op.drop_column("traffic_pack_points_mode")
        batch_op.drop_column("allow_traffic_pack")
        batch_op.drop_column("request_points_ratio")
        batch_op.drop_column("request_points_fixed")
        batch_op.drop_column("request_points_mode")
        batch_op.alter_column(
            "allow_request",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("(true)"),
            existing_server_default=sa.text("false"),
        )
