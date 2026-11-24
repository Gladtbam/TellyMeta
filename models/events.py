"""全系统统一通知事件枚举、路由分类与元数据注册表"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class NotificationRouteCategory(StrEnum):
    """通知路由所属业务大类"""

    MEDIA_LIBRARY = "media_library"
    MEDIA_REQUEST = "media_request"
    ACCOUNT_LIFECYCLE = "account_lifecycle"
    OPS_ALERT = "ops_alert"
    COMMUNITY = "community"


class NotificationAudience(StrEnum):
    """通知事件投递受众模式"""

    TOPIC = "topic"
    USER_DM = "user_dm"
    OPS = "ops"
    BOTH = "both"


class NotificationSeverity(StrEnum):
    """通知事件严重程度级别"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class NotificationRouteScope(StrEnum):
    """通知路由作用域"""

    GLOBAL = "global"
    SERVER = "server"


class NotificationTargetType(StrEnum):
    """通知路由 Telegram 投递目标类型"""

    DISABLED = "disabled"
    MAIN_CHAT = "main_chat"
    FORUM_TOPIC = "forum_topic"
    CHAT_OR_CHANNEL = "chat_or_channel"
    ADMIN_DM_GROUP = "admin_dm_group"


class NotificationEvent(StrEnum):
    """系统内部统一通知事件枚举"""

    # --- 媒体入库与 Arr 媒体库事件 (media_library) ---
    SONARR_DOWNLOAD = "sonarr_download"
    SONARR_SERIES_ADD = "sonarr_series_add"
    SONARR_GRAB = "sonarr_grab"
    SONARR_RENAME = "sonarr_rename"
    SONARR_DELETE = "sonarr_delete"
    RADARR_DOWNLOAD = "radarr_download"
    RADARR_MOVIE_ADD = "radarr_movie_add"
    RADARR_GRAB = "radarr_grab"
    RADARR_RENAME = "radarr_rename"
    RADARR_DELETE = "radarr_delete"
    EMBY_LIBRARY_NEW = "emby_library_new"
    JELLYFIN_LIBRARY_NEW = "jellyfin_library_new"
    MEDIA_LIBRARY_DELETED = "media_library_deleted"

    # --- 求片系统事件 (media_request) ---
    REQUEST_SUBMIT = "request_submit"
    REQUEST_APPROVED = "request_approved"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_COMPLETED = "request_completed"
    REQUEST_ARR_FAILED = "request_arr_failed"
    REQUEST_EXPIRED = "request_expired"

    # --- 媒体账号生命周期与管理回执 (account_lifecycle) ---
    PRE_EXPIRY_REMINDER = "pre_expiry_reminder"
    AUTO_RENEW_SUCCESS = "auto_renew_success"
    AUTO_RENEW_FAILED = "auto_renew_failed"
    EXPIRY_BAN = "expiry_ban"
    GRACE_DELETE_REMINDER = "grace_delete_reminder"
    ACCOUNT_DELETED = "account_deleted"
    TRAFFIC_QUOTA_80 = "traffic_quota_80"
    TRAFFIC_QUOTA_100 = "traffic_quota_100"
    TRAFFIC_AUTO_SUSPENDED = "traffic_auto_suspended"
    TRAFFIC_UNSUSPENDED = "traffic_unsuspended"
    WATCH_TIME_RENEWAL_ELIGIBLE = "watch_time_renewal_eligible"
    BILLING_CYCLE_RESET = "billing_cycle_reset"
    ADMIN_USER_ACTION = "admin_user_action"
    SELF_ACTION_RECEIPT = "self_action_receipt"

    # --- 系统运维监控与后台任务告警 (ops_alert) ---
    ARR_MANUAL_INTERACTION = "arr_manual_interaction"
    ARR_HEALTH_ISSUE = "arr_health_issue"
    ARR_HEALTH_RESTORED = "arr_health_restored"
    ARR_TEST = "arr_test"
    MEDIA_AUTH_RISK = "media_auth_risk"
    MEDIA_USER_LOCKED_OUT = "media_user_locked_out"
    MEDIA_TASK_FAILED = "media_task_failed"
    MEDIA_SUBTITLE_FAILED = "media_subtitle_failed"
    MEDIA_SYSTEM_TEST = "media_system_test"
    MKV_PROCESS_FAILED = "mkv_process_failed"
    MKV_PATH_UNMAPPED = "mkv_path_unmapped"
    NFO_PROCESS_FAILED = "nfo_process_failed"
    TRANSLATOR_ERROR = "translator_error"
    QB_SHARE_LIMIT_FAILED = "qb_share_limit_failed"
    EXTERNAL_SERVER_ERROR = "external_server_error"
    EXTERNAL_SERVER_RECOVERED = "external_server_recovered"
    LIFECYCLE_RETRY_ALERT = "lifecycle_retry_alert"
    NSFW_REVOKE_RETRY_ALERT = "nsfw_revoke_retry_alert"
    DB_BACKUP_FAILED = "db_backup_failed"
    CLEANUP_INACTIVE_SUMMARY = "cleanup_inactive_summary"
    STARTUP_CALIBRATION_SUMMARY = "startup_calibration_summary"
    ORPHAN_SESSIONS_SUMMARY = "orphan_sessions_summary"
    USER_POLICY_SYNC_SUMMARY = "user_policy_sync_summary"
    BATCH_TRAFFIC_RELEASE_SUMMARY = "batch_traffic_release_summary"
    TELEGRAM_DELIVERY_DEGRADED = "telegram_delivery_degraded"

    # --- 社区积分与群组治理事件 (community) ---
    SCORE_SETTLEMENT_REPORT = "score_settlement_report"
    VERIFICATION_REJECT_AUDIT = "verification_reject_audit"
    MEMBER_LEFT_CLEANUP_AUDIT = "member_left_cleanup_audit"

    # --- 系统通用通路测试事件 ---
    SYSTEM_TEST = "system_test"


@dataclass(frozen=True, slots=True)
class NotificationEventSpec:
    """单个通知事件的领域元数据描述"""

    event: NotificationEvent
    category: NotificationRouteCategory
    audience: NotificationAudience
    severity: NotificationSeverity
    is_mandatory: bool = False
    preference_key: str | None = None
    default_enabled: bool = True
    supports_cooldown: bool = False


def _spec(
    event: NotificationEvent,
    category: NotificationRouteCategory,
    audience: NotificationAudience,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    *,
    is_mandatory: bool = False,
    preference_key: str | None = None,
    default_enabled: bool = True,
    supports_cooldown: bool = False,
) -> tuple[NotificationEvent, NotificationEventSpec]:
    """构造事件元数据注册条目"""
    return (
        event,
        NotificationEventSpec(
            event=event,
            category=category,
            audience=audience,
            severity=severity,
            is_mandatory=is_mandatory,
            preference_key=preference_key,
            default_enabled=default_enabled,
            supports_cooldown=supports_cooldown,
        ),
    )


NOTIFICATION_EVENT_SPECS: dict[NotificationEvent, NotificationEventSpec] = dict(
    [
        # 媒体库与 Arr（默认开启入库与新增剧集/电影；抓取/重命名/删除默认按需勾选）
        _spec(
            NotificationEvent.SONARR_DOWNLOAD,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.SONARR_SERIES_ADD,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.SONARR_GRAB,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.SONARR_RENAME,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.SONARR_DELETE,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            NotificationSeverity.WARNING,
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.RADARR_DOWNLOAD,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.RADARR_MOVIE_ADD,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.RADARR_GRAB,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.RADARR_RENAME,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.RADARR_DELETE,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            NotificationSeverity.WARNING,
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.EMBY_LIBRARY_NEW,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.JELLYFIN_LIBRARY_NEW,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.MEDIA_LIBRARY_DELETED,
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationAudience.TOPIC,
            NotificationSeverity.WARNING,
            default_enabled=False,
        ),
        # 求片审核与状态回执
        _spec(
            NotificationEvent.REQUEST_SUBMIT,
            NotificationRouteCategory.MEDIA_REQUEST,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.REQUEST_APPROVED,
            NotificationRouteCategory.MEDIA_REQUEST,
            NotificationAudience.USER_DM,
        ),
        _spec(
            NotificationEvent.REQUEST_REJECTED,
            NotificationRouteCategory.MEDIA_REQUEST,
            NotificationAudience.USER_DM,
            NotificationSeverity.WARNING,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.REQUEST_COMPLETED,
            NotificationRouteCategory.MEDIA_REQUEST,
            NotificationAudience.USER_DM,
            NotificationSeverity.INFO,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.REQUEST_ARR_FAILED,
            NotificationRouteCategory.MEDIA_REQUEST,
            NotificationAudience.USER_DM,
            NotificationSeverity.WARNING,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.REQUEST_EXPIRED,
            NotificationRouteCategory.MEDIA_REQUEST,
            NotificationAudience.USER_DM,
            NotificationSeverity.WARNING,
            is_mandatory=True,
        ),
        # 账号生命周期（用户侧事件默认投递私聊；ADMIN_USER_ACTION 双发用户回执与管理审计）
        _spec(
            NotificationEvent.PRE_EXPIRY_REMINDER,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.WARNING,
            preference_key="allow_pre_expiry_reminder",
            default_enabled=True,
        ),
        _spec(
            NotificationEvent.AUTO_RENEW_SUCCESS,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
        ),
        _spec(
            NotificationEvent.AUTO_RENEW_FAILED,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.CRITICAL,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.EXPIRY_BAN,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.BOTH,
            NotificationSeverity.CRITICAL,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.GRACE_DELETE_REMINDER,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.CRITICAL,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.ACCOUNT_DELETED,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.BOTH,
            NotificationSeverity.CRITICAL,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.TRAFFIC_QUOTA_80,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.WARNING,
            preference_key="allow_traffic_80_alert",
            default_enabled=True,
        ),
        _spec(
            NotificationEvent.TRAFFIC_QUOTA_100,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.CRITICAL,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.TRAFFIC_AUTO_SUSPENDED,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.CRITICAL,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.TRAFFIC_UNSUSPENDED,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.INFO,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.WATCH_TIME_RENEWAL_ELIGIBLE,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.INFO,
            preference_key="allow_watch_time_milestone",
            default_enabled=True,
        ),
        _spec(
            NotificationEvent.BILLING_CYCLE_RESET,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.INFO,
            preference_key="allow_cycle_reset",
            default_enabled=False,
        ),
        _spec(
            NotificationEvent.ADMIN_USER_ACTION,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.BOTH,
            NotificationSeverity.WARNING,
            is_mandatory=True,
        ),
        _spec(
            NotificationEvent.SELF_ACTION_RECEIPT,
            NotificationRouteCategory.ACCOUNT_LIFECYCLE,
            NotificationAudience.USER_DM,
            NotificationSeverity.INFO,
            preference_key="allow_self_action_receipt",
            default_enabled=True,
        ),
        # 系统运维告警与后台摘要
        _spec(
            NotificationEvent.ARR_MANUAL_INTERACTION,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.ARR_HEALTH_ISSUE,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.ARR_HEALTH_RESTORED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.ARR_TEST,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.MEDIA_AUTH_RISK,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.CRITICAL,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.MEDIA_USER_LOCKED_OUT,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.MEDIA_TASK_FAILED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.MEDIA_SUBTITLE_FAILED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.MEDIA_SYSTEM_TEST,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.MKV_PROCESS_FAILED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.MKV_PATH_UNMAPPED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.NFO_PROCESS_FAILED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.TRANSLATOR_ERROR,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.QB_SHARE_LIMIT_FAILED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.WARNING,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.EXTERNAL_SERVER_ERROR,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.CRITICAL,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.EXTERNAL_SERVER_RECOVERED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.LIFECYCLE_RETRY_ALERT,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.CRITICAL,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.NSFW_REVOKE_RETRY_ALERT,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.CRITICAL,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.DB_BACKUP_FAILED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.CRITICAL,
            supports_cooldown=True,
        ),
        _spec(
            NotificationEvent.CLEANUP_INACTIVE_SUMMARY,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.STARTUP_CALIBRATION_SUMMARY,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.ORPHAN_SESSIONS_SUMMARY,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.USER_POLICY_SYNC_SUMMARY,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.BATCH_TRAFFIC_RELEASE_SUMMARY,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
        _spec(
            NotificationEvent.TELEGRAM_DELIVERY_DEGRADED,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
            NotificationSeverity.CRITICAL,
            supports_cooldown=True,
        ),
        # 社区积分与群组治理
        _spec(
            NotificationEvent.SCORE_SETTLEMENT_REPORT,
            NotificationRouteCategory.COMMUNITY,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.VERIFICATION_REJECT_AUDIT,
            NotificationRouteCategory.COMMUNITY,
            NotificationAudience.TOPIC,
            NotificationSeverity.WARNING,
        ),
        _spec(
            NotificationEvent.MEMBER_LEFT_CLEANUP_AUDIT,
            NotificationRouteCategory.COMMUNITY,
            NotificationAudience.TOPIC,
        ),
        # 系统路由实测事件（默认归属 ops_alert，测试特定类别路由时按目标路由覆盖）
        _spec(
            NotificationEvent.SYSTEM_TEST,
            NotificationRouteCategory.OPS_ALERT,
            NotificationAudience.OPS,
        ),
    ]
)

USER_PREFERENCE_KEYS: tuple[str, ...] = (
    "allow_watch_time_milestone",
    "allow_pre_expiry_reminder",
    "allow_traffic_80_alert",
    "allow_cycle_reset",
    "allow_self_action_receipt",
)

DEFAULT_GLOBAL_ROUTE_TARGETS: dict[
    NotificationRouteCategory, NotificationTargetType
] = {
    NotificationRouteCategory.MEDIA_LIBRARY: NotificationTargetType.DISABLED,
    NotificationRouteCategory.MEDIA_REQUEST: NotificationTargetType.DISABLED,
    NotificationRouteCategory.ACCOUNT_LIFECYCLE: NotificationTargetType.DISABLED,
    NotificationRouteCategory.OPS_ALERT: NotificationTargetType.ADMIN_DM_GROUP,
    NotificationRouteCategory.COMMUNITY: NotificationTargetType.MAIN_CHAT,
}


def get_event_spec(event: NotificationEvent | str) -> NotificationEventSpec:
    """根据通知事件枚举或字符串标识查询元数据描述

    Args:
        event: 通知事件枚举或其字符串值。

    Returns:
        对应的 ``NotificationEventSpec`` 元数据对象。

    Raises:
        ValueError: 当传入未注册的事件标识时抛出。
    """
    normalized = (
        event if isinstance(event, NotificationEvent) else NotificationEvent(event)
    )
    return NOTIFICATION_EVENT_SPECS[normalized]


def get_optional_events_for_category(
    category: NotificationRouteCategory | str,
) -> tuple[NotificationEvent, ...]:
    """列出指定路由类别下默认关闭、需路由显式勾选启用的可选事件

    Args:
        category: 路由类别枚举或字符串标识。

    Returns:
        属于该类别且 ``default_enabled=False`` 的事件元组。
    """
    normalized = (
        category
        if isinstance(category, NotificationRouteCategory)
        else NotificationRouteCategory(category)
    )
    return tuple(
        spec.event
        for spec in NOTIFICATION_EVENT_SPECS.values()
        if spec.category == normalized and not spec.default_enabled
    )


# 路由类别展示元信息（标题、说明与允许使用的目标类型）
ROUTE_CATEGORY_LABELS: dict[NotificationRouteCategory, tuple[str, str]] = {
    NotificationRouteCategory.MEDIA_LIBRARY: (
        "媒体入库与 Arr 动态",
        "Emby / Jellyfin 新片入库、Sonarr / Radarr 订阅与下载通知",
    ),
    NotificationRouteCategory.MEDIA_REQUEST: (
        "求片审核卡片",
        "向管理员发送待审核求片卡片并同步更新批准/拒绝/入库状态",
    ),
    NotificationRouteCategory.ACCOUNT_LIFECYCLE: (
        "账号生命周期与管理审计",
        "过期封禁、自动删号及管理员操作等管理侧审计同步",
    ),
    NotificationRouteCategory.OPS_ALERT: (
        "系统运维告警",
        "媒体服/Arr/qB 离线、Arr 健康告警、MKV/NFO/翻译故障与任务摘要",
    ),
    NotificationRouteCategory.COMMUNITY: (
        "社区积分与群组治理",
        "每日发言积分结算榜单、入群验证拒审与退群自动清理审计",
    ),
}

# 默认关闭、需用户在路由白名单中主动勾选启用的可选事件中文标题
OPTIONAL_EVENT_LABELS: dict[NotificationEvent, str] = {
    NotificationEvent.SONARR_GRAB: "Sonarr 抓取种子 (Grab)",
    NotificationEvent.SONARR_RENAME: "Sonarr 文件重命名 (Rename)",
    NotificationEvent.SONARR_DELETE: "Sonarr 剧集/文件删除 (Delete)",
    NotificationEvent.RADARR_GRAB: "Radarr 抓取种子 (Grab)",
    NotificationEvent.RADARR_RENAME: "Radarr 文件重命名 (Rename)",
    NotificationEvent.RADARR_DELETE: "Radarr 电影/文件删除 (Delete)",
    NotificationEvent.MEDIA_LIBRARY_DELETED: "Emby/Jellyfin 媒体删除",
}

# 允许路由到管理员私聊组 (admin_dm_group) 的全局类别集合
ADMIN_DM_ALLOWED_CATEGORIES: frozenset[NotificationRouteCategory] = frozenset(
    {
        NotificationRouteCategory.OPS_ALERT,
        NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    }
)
