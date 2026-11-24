from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


class ServerType(StrEnum):
    """服务类型枚举"""

    EMBY = "emby"
    JELLYFIN = "jellyfin"
    SONARR = "sonarr"
    RADARR = "radarr"
    QBITTORRENT = "qbittorrent"


class RegistrationMode(StrEnum):
    """注册模式枚举"""

    DEFAULT = "default"  # 仅邀请/积分
    OPEN = "open"  # 开放注册
    COUNT = "count"  # 限制名额
    TIME = "time"  # 限时开放
    CLOSE = "close"  # 完全关闭 (仅管理员添加)
    EXTERNAL = "external"  # 外部验证


class TelegramUser(Base):
    """Telegram用户模型"""

    __tablename__ = "telegram_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_admin: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    score: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    checkin_count: Mapped[int] = mapped_column(
        server_default=text("0"), nullable=False
    )  # 累计签到
    consecutive_checkin_count: Mapped[int] = mapped_column(
        server_default=text("0"), nullable=False
    )  # 连续签到
    warning_count: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    last_checkin: Mapped[datetime] = mapped_column(
        default=datetime(1970, 1, 1), nullable=False
    )

    @property
    def current_consecutive_checkin_count(self) -> int:
        """获取当前有效连续签到天数"""
        if not self.last_checkin:
            return 0
        today = date.today()
        if self.last_checkin.date() < (today - timedelta(days=1)):
            return 0
        return self.consecutive_checkin_count

    media_users: Mapped[list[MediaUser]] = relationship(
        back_populates="telegram_user", cascade="all, delete-orphan", lazy="selectin"
    )


class ServerInstance(Base):
    """服务器实例配置模型"""

    __tablename__ = "server_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    server_type: Mapped[str] = mapped_column(String(32), nullable=True)
    url: Mapped[str] = mapped_column(String(255), nullable=True)
    api_key: Mapped[str] = mapped_column(String(255), nullable=True)
    webhook_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    priority: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )  # 优先级
    tos: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    notify_topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Jellyfin/Emby 相关配置
    registration_mode: Mapped[str] = mapped_column(
        String(32), default=RegistrationMode.DEFAULT, server_default=text("'default'")
    )
    registration_count_limit: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    registration_time_limit: Mapped[str] = mapped_column(
        String(32), default="0", server_default=text("'0'")
    )
    registration_expiry_months: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )  # 账号有效自然月数
    registration_external_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    registration_external_parser: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    code_expiry_days: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30")
    )
    nsfw_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("false")
    )
    nsfw_library_ids: Mapped[str] = mapped_column(
        String(1024), default="", server_default=text("''")
    )
    nsfw_sub_library_ids: Mapped[str] = mapped_column(
        String(2048), default="", server_default=text("''")
    )
    allow_subtitle_upload: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    allow_request: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 服务器独立求片定价模式与参数（新服默认 fixed / 0 分；0 分求片不依赖全局积分系统）
    request_points_mode: Mapped[str] = mapped_column(
        String(16), default="fixed", server_default=text("'fixed'"), nullable=False
    )
    request_points_fixed: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    request_points_ratio: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0.0"), nullable=False
    )
    allow_nfo: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    # 服务器级 MKV 自动清理与 LLM 元数据翻译子开关（分别作用于 Arr 与媒体服务器，默认开启以兼容存量实例）
    enable_mkv_process: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    enable_llm_translate: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    enable_watch_time: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    # 服务器级时长保活续期开关（默认关闭，实现观测与保活解耦灰度上线）
    enable_watch_time_renewal: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 时长保活续期门槛秒数（0 表示未设定门槛）
    watch_time_renewal_threshold_seconds: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # 服务器级流量采集、超流告警、超流自动停用与月基础流量配额（字节，0 表示不限）
    enable_traffic: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    enable_traffic_alert: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    enable_traffic_auto_suspend: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    traffic_quota_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # 服务器独立流量包开关、单价与单包容量配置（新服默认关闭，仅当开启积分与限流后生效，最低 1 积分）
    allow_traffic_pack: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    traffic_pack_points_mode: Mapped[str] = mapped_column(
        String(16), default="fixed", server_default=text("'fixed'"), nullable=False
    )
    traffic_pack_points_fixed: Mapped[int] = mapped_column(
        Integer, default=50, server_default=text("50"), nullable=False
    )
    traffic_pack_points_ratio: Mapped[float] = mapped_column(
        Float, default=0.5, server_default=text("0.5"), nullable=False
    )
    traffic_pack_size_gib: Mapped[float] = mapped_column(
        Float, default=10.0, server_default=text("10.0"), nullable=False
    )
    # 媒体服务器默认账号 UserPolicy 模板（JSON 字符串，按服务器独立配置）
    default_user_policy: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Radarr/Sonarr 路径映射: JSON 字符串: {"/remote/path": "/local/path"}
    path_mappings: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    request_notify_topic_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )

    # qBittorrent 实例配置与 Radarr/Sonarr 绑定关系
    qb_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qb_torrent_limit: Mapped[int] = mapped_column(
        Integer, default=43200, server_default=text("43200")
    )
    qb_server_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("server_instances.id", ondelete="SET NULL"),
        nullable=True,
    )


class MediaUser(Base):
    """Media 用户模型"""

    __tablename__ = "media_user"

    id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("telegram_users.id"), primary_key=True
    )
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("server_instances.id", ondelete="CASCADE"), primary_key=True
    )
    media_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    media_name: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    anchor_day: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # 锚定基准日号数（1..31）
    cycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # 当前自然月周期起始时间
    next_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # 下一次月度重置时间
    is_banned: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    delete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cycle_watch_time_seconds: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    total_watch_time_seconds: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    watch_time_remainder_ms: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # 用户可控自动续期开关（默认关闭；开启后到达有效期时按服务器准许条件自动续期）
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 本周期基础流量消耗、当期临时调额、当期流量包余额与历史总流量消耗（字节），以及告警去重与超流停用隔离标记
    cycle_traffic_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # 管理员当期临时调额字节数（支持正负加减，随当期重置或过期重锚一并清零）
    cycle_traffic_adjustment_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    traffic_pack_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    total_traffic_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    traffic_alert_80_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    traffic_alert_100_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 独立于过期封禁 is_banned 与 delete_at 的超流停用标记（遵循 ADR-0002）
    is_traffic_suspended: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 同有效期周期内是否已触发保活首次达标提醒，及宽限删号前 24h 最后提醒标记
    watch_time_milestone_notified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    grace_delete_reminder_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    telegram_user: Mapped[TelegramUser] = relationship(back_populates="media_users")
    __table_args__ = (
        UniqueConstraint("server_id", "media_id", name="uq_server_media_id"),
    )


class ActiveCode(Base):
    """激活码模型"""

    __tablename__ = "active_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("server_instances.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

    server: Mapped[ServerInstance] = relationship()


class PendingVerification(Base):
    """待验证用户模型"""

    __tablename__ = "pending_verifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    captcha_answer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scheduler_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class BotConfiguration(Base):
    """Bot 配置模型"""

    __tablename__ = "bot_configurations"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class LibraryBinding(Base):
    """媒体库绑定模型 - 将 Emby/Jellyfin 媒体库绑定到 Sonarr/Radarr 实例"""

    __tablename__ = "library_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    library_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("server_instances.id", ondelete="CASCADE"), nullable=False
    )
    media_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("server_instances.id", ondelete="CASCADE"), nullable=False
    )
    quality_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    root_folder: Mapped[str] = mapped_column(String(512), nullable=False)

    arr_server: Mapped[ServerInstance] = relationship(foreign_keys=[arr_id])
    media_server: Mapped[ServerInstance] = relationship(foreign_keys=[media_id])

    __table_args__ = (
        UniqueConstraint("media_id", "library_name", name="uq_media_library"),
    )


class ApiCache(Base):
    """API 响应缓存模型"""

    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)  # JSON data
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MediaRequest(Base):
    """求片申请与积分预留追踪模型"""

    __tablename__ = "media_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    media_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("server_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    arr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("server_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    library_name: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    media_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 提交时锁定的预留积分，后续动态改价不追补或改扣
    reserved_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 请求状态：pending / approved / rejected / canceled / expired / failed
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    # 积分状态：reserved / settled / released
    cost_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved"
    )
    topic_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approver_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 媒体库真实入库并完成 REQUEST_COMPLETED 通知的时间（用于多集剧集入库防重复触发）
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 同一 Arr 实例对同一媒体同时仅允许一条待审核求片
        Index(
            "uq_pending_arr_media",
            "arr_id",
            "media_type",
            "media_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )


class MediaWatchDaily(Base):
    """媒体账号按自然日有效播放时长累计表（用于单日上限控制）"""

    __tablename__ = "media_watch_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    watch_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    watch_seconds: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "server_id"],
            ["media_user.id", "media_user.server_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "server_id",
            "watch_date",
            name="uq_media_watch_daily_account_date",
        ),
    )


class MediaWatchInterval(Base):
    """媒体账号已计入的有效播放时间线区间（合并去重用）"""

    __tablename__ = "media_watch_intervals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "server_id"],
            ["media_user.id", "media_user.server_id"],
            ondelete="CASCADE",
        ),
    )


class MediaPlaybackSession(Base):
    """媒体播放会话快照表（支撑暂停剔除、Seek 识别、防刷幂等与重启恢复）"""

    __tablename__ = "media_playback_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_key: Mapped[str] = mapped_column(String(128), nullable=False)
    play_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False, index=True
    )
    is_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    last_position_ticks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_effective_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "server_id"],
            ["media_user.id", "media_user.server_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "server_id",
            "session_key",
            name="uq_media_playback_session_server_key",
        ),
    )


class NotificationRoute(Base):
    """分层通知路由配置模型（支持全局默认规则与按服务器实例覆盖）"""

    __tablename__ = "notification_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    server_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("server_instances.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(
        String(32),
        default="disabled",
        server_default=text("'disabled'"),
        nullable=False,
    )
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    escalate_admin_dm: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 可选高频或删除类事件白名单 JSON 列表（如 ["sonarr_grab", "media_library_deleted"]）
    enabled_events_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    __table_args__ = (
        # SQLite/PostgreSQL 下 NULL 值在普通唯一约束中不冲突，拆分为两个部分唯一索引以防重复路由
        Index(
            "uq_notification_route_global_cat",
            "scope_type",
            "category",
            unique=True,
            sqlite_where=text("server_id IS NULL"),
            postgresql_where=text("server_id IS NULL"),
        ),
        Index(
            "uq_notification_route_server_cat",
            "scope_type",
            "server_id",
            "category",
            unique=True,
            sqlite_where=text("server_id IS NOT NULL"),
            postgresql_where=text("server_id IS NOT NULL"),
        ),
    )


class NotificationInboxItem(Base):
    """用户私聊投递失败兜底站内收件箱模型（仅存脱敏摘要，保留 30 天）"""

    __tablename__ = "notification_inbox_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    server_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("server_instances.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), default="info", server_default=text("'info'"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    action_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    is_read: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
        index=True,
    )
    delivery_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False, index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OpsAlertLog(Base):
    """系统运维告警记录模型（支撑同源冷却去重与 30 天审计查询）"""

    __tablename__ = "ops_alert_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    server_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("server_instances.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), default="warning", server_default=text("'warning'"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_targets_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default=text("'[]'"), nullable=False
    )
    delivery_status: Mapped[str] = mapped_column(
        String(32), default="sent", server_default=text("'sent'"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False, index=True
    )


class UserNotificationPreference(Base):
    """Telegram 用户可选通知偏好配置模型（必达通知不受本表开关限制）"""

    __tablename__ = "user_notification_preferences"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    allow_watch_time_milestone: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    allow_pre_expiry_reminder: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    allow_traffic_80_alert: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    allow_cycle_reset: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    allow_self_action_receipt: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
