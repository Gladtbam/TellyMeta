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
    COMMUNITY_SCORE = "community_score"
    COMMUNITY_VERIFICATION = "community_verification"
    COMMUNITY_LEAVE_AUDIT = "community_leave_audit"


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
        # 社区积分与群组治理（按事件细粒度拆分独立全局路由）
        _spec(
            NotificationEvent.SCORE_SETTLEMENT_REPORT,
            NotificationRouteCategory.COMMUNITY_SCORE,
            NotificationAudience.TOPIC,
        ),
        _spec(
            NotificationEvent.VERIFICATION_REJECT_AUDIT,
            NotificationRouteCategory.COMMUNITY_VERIFICATION,
            NotificationAudience.TOPIC,
            NotificationSeverity.WARNING,
        ),
        _spec(
            NotificationEvent.MEMBER_LEFT_CLEANUP_AUDIT,
            NotificationRouteCategory.COMMUNITY_LEAVE_AUDIT,
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

# 全局通知路由矩阵支持的业务类别（媒体入库与求片不设全局总开关，交由各服务器独立配置）
GLOBAL_ROUTE_CATEGORIES: tuple[NotificationRouteCategory, ...] = (
    NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    NotificationRouteCategory.OPS_ALERT,
    NotificationRouteCategory.COMMUNITY_SCORE,
    NotificationRouteCategory.COMMUNITY_VERIFICATION,
    NotificationRouteCategory.COMMUNITY_LEAVE_AUDIT,
)

# 仅归属单台服务器、不继承全局总开关且默认关闭通知的路由类别（媒体服/Arr/qB）
SERVER_OWNED_ROUTE_CATEGORIES: frozenset[NotificationRouteCategory] = frozenset(
    {
        NotificationRouteCategory.MEDIA_LIBRARY,
        NotificationRouteCategory.MEDIA_REQUEST,
        NotificationRouteCategory.OPS_ALERT,
    }
)

# 允许单服配置但未配置时可继承全局默认的路由类别
SERVER_INHERITABLE_ROUTE_CATEGORIES: frozenset[NotificationRouteCategory] = frozenset(
    {
        NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    }
)

# 单服「通知」页签全集类别顺序
SERVER_ROUTE_CATEGORIES: tuple[NotificationRouteCategory, ...] = (
    NotificationRouteCategory.MEDIA_LIBRARY,
    NotificationRouteCategory.MEDIA_REQUEST,
    NotificationRouteCategory.OPS_ALERT,
    NotificationRouteCategory.ACCOUNT_LIFECYCLE,
)

# 按服务器类型声明适用的单服通知类别顺序
SERVER_TYPE_ROUTE_CATEGORIES: dict[str, tuple[NotificationRouteCategory, ...]] = {
    "emby": (
        NotificationRouteCategory.MEDIA_LIBRARY,
        NotificationRouteCategory.OPS_ALERT,
        NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    ),
    "jellyfin": (
        NotificationRouteCategory.MEDIA_LIBRARY,
        NotificationRouteCategory.OPS_ALERT,
        NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    ),
    "sonarr": (
        NotificationRouteCategory.MEDIA_LIBRARY,
        NotificationRouteCategory.MEDIA_REQUEST,
        NotificationRouteCategory.OPS_ALERT,
    ),
    "radarr": (
        NotificationRouteCategory.MEDIA_LIBRARY,
        NotificationRouteCategory.MEDIA_REQUEST,
        NotificationRouteCategory.OPS_ALERT,
    ),
    "qbittorrent": (NotificationRouteCategory.OPS_ALERT,),
}

DEFAULT_GLOBAL_ROUTE_TARGETS: dict[
    NotificationRouteCategory, NotificationTargetType
] = {
    NotificationRouteCategory.ACCOUNT_LIFECYCLE: NotificationTargetType.DISABLED,
    NotificationRouteCategory.OPS_ALERT: NotificationTargetType.ADMIN_DM_GROUP,
    NotificationRouteCategory.COMMUNITY_SCORE: NotificationTargetType.MAIN_CHAT,
    NotificationRouteCategory.COMMUNITY_VERIFICATION: NotificationTargetType.ADMIN_DM_GROUP,
    NotificationRouteCategory.COMMUNITY_LEAVE_AUDIT: NotificationTargetType.ADMIN_DM_GROUP,
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
    """列出指定路由类别下默认关闭、需在管理路由白名单中显式勾选启用的可选事件

    仅包含实际经由管理/群组路由投递的事件（自动排除仅受用户个人偏好控制的纯私聊 ``USER_DM`` 事件）。

    Args:
        category: 路由类别枚举或字符串标识。

    Returns:
        属于该类别、非纯用户私聊且 ``default_enabled=False`` 的事件元组。
    """
    normalized = (
        category
        if isinstance(category, NotificationRouteCategory)
        else NotificationRouteCategory(category)
    )
    return tuple(
        spec.event
        for spec in NOTIFICATION_EVENT_SPECS.values()
        if spec.category == normalized
        and spec.audience != NotificationAudience.USER_DM
        and not spec.default_enabled
    )


# 路由类别展示元信息（全局视角：标题与说明）
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
        "系统全局运维告警",
        "DB 备份失败、退群清理/启动校准/孤立会话摘要与投递降级（媒体服/Arr/qB 告警请在各服务器「通知」页签单独配置）",
    ),
    NotificationRouteCategory.COMMUNITY_SCORE: (
        "每日发言积分结算榜单",
        "定时或手动 /settle 触发的群聊发言积分结算播报",
    ),
    NotificationRouteCategory.COMMUNITY_VERIFICATION: (
        "入群验证拒审审计",
        "新成员入群验证超时或管理员拒审踢封审计通知",
    ),
    NotificationRouteCategory.COMMUNITY_LEAVE_AUDIT: (
        "退群自动清理审计",
        "群成员退群或被移出后自动清理媒体账号的审计通知",
    ),
}

# 单服「通知」页签通用兜底类别展示标题与说明
SERVER_ROUTE_CATEGORY_LABELS: dict[NotificationRouteCategory, tuple[str, str]] = {
    NotificationRouteCategory.MEDIA_LIBRARY: (
        "媒体库动态",
        "当前服务器的媒体入库或下载动态（默认关闭，不继承全局）",
    ),
    NotificationRouteCategory.MEDIA_REQUEST: (
        "求片审核",
        "指向当前实例的待审核求片卡片与状态回写（默认关闭，不继承全局）",
    ),
    NotificationRouteCategory.OPS_ALERT: (
        "服务运维告警",
        "当前服务器的离线异常、健康告警与任务报错通知（默认关闭，不继承全局）",
    ),
    NotificationRouteCategory.ACCOUNT_LIFECYCLE: (
        "账号生命周期与管理审计",
        "本服账号过期封禁、删号与管理员操作审计（默认跟随全局，可按服覆盖）",
    ),
}

# 媒体服务器（Emby / Jellyfin）共享的单服通知类别标题与说明
_MEDIA_SERVER_ROUTE_CATEGORY_LABELS: dict[
    NotificationRouteCategory, tuple[str, str]
] = {
    NotificationRouteCategory.MEDIA_LIBRARY: (
        "媒体入库通知",
        "新片与剧集入库播报（默认关闭，可按需勾选媒体删除）",
    ),
    NotificationRouteCategory.OPS_ALERT: (
        "媒体服运维告警",
        "接口离线与恢复、异常登录风控、账号锁定、计划任务/字幕异常、翻译报错与策略同步摘要（默认关闭）",
    ),
    NotificationRouteCategory.ACCOUNT_LIFECYCLE: (
        "账号生命周期与管理审计",
        "本服账号过期封禁、删号与管理员操作审计（默认跟随全局，可按服覆盖）",
    ),
}

# 按服务器类型定制的单服「通知」页签类别标题与说明
SERVER_TYPE_ROUTE_CATEGORY_LABELS: dict[
    str, dict[NotificationRouteCategory, tuple[str, str]]
] = {
    "emby": _MEDIA_SERVER_ROUTE_CATEGORY_LABELS,
    "jellyfin": _MEDIA_SERVER_ROUTE_CATEGORY_LABELS,
    "sonarr": {
        NotificationRouteCategory.MEDIA_LIBRARY: (
            "剧集订阅与下载动态",
            "新增剧集订阅与下载完成导入通知（默认关闭，可按需勾选抓取、重命名或删除）",
        ),
        NotificationRouteCategory.MEDIA_REQUEST: (
            "剧集求片审核",
            "指向本实例的剧集求片审核卡片与审批状态同步（默认关闭）",
        ),
        NotificationRouteCategory.OPS_ALERT: (
            "服务运维告警",
            "接口连接异常与恢复、健康检查告警、导入需人工处理及 MKV/NFO 后处理异常（默认关闭）",
        ),
    },
    "radarr": {
        NotificationRouteCategory.MEDIA_LIBRARY: (
            "电影订阅与下载动态",
            "新增电影订阅与下载完成导入通知（默认关闭，可按需勾选抓取、重命名或删除）",
        ),
        NotificationRouteCategory.MEDIA_REQUEST: (
            "电影求片审核",
            "指向本实例的电影求片审核卡片与审批状态同步（默认关闭）",
        ),
        NotificationRouteCategory.OPS_ALERT: (
            "服务运维告警",
            "接口连接异常与恢复、健康检查告警、导入需人工处理及 MKV/NFO 后处理异常（默认关闭）",
        ),
    },
    "qbittorrent": {
        NotificationRouteCategory.OPS_ALERT: (
            "下载器运维告警",
            "下载器离线与恢复、做种分享率与限速规则执行失败告警（默认关闭）",
        ),
    },
}


def get_server_route_category_label(
    server_type: str | None, category: NotificationRouteCategory
) -> tuple[str, str]:
    """根据服务器类型与路由类别返回定制的展示标题与说明。

    Args:
        server_type: 服务器类型字符串（如 emby/jellyfin/sonarr/radarr/qbittorrent）。
        category: 通知路由类别枚举。

    Returns:
        ``(标题, 说明)`` 二元组；未命中按类型定制文案时回退到单服通用兜底文案。
    """
    normalized_type = str(server_type or "").lower()
    type_labels = SERVER_TYPE_ROUTE_CATEGORY_LABELS.get(normalized_type)
    if type_labels and category in type_labels:
        return type_labels[category]
    return SERVER_ROUTE_CATEGORY_LABELS.get(category, ROUTE_CATEGORY_LABELS[category])


# 默认关闭、需用户在对应服务器路由白名单中主动勾选启用的可选事件中文标题
OPTIONAL_EVENT_LABELS: dict[NotificationEvent, str] = {
    NotificationEvent.SONARR_GRAB: "抓取种子",
    NotificationEvent.SONARR_RENAME: "文件重命名",
    NotificationEvent.SONARR_DELETE: "剧集/文件删除",
    NotificationEvent.RADARR_GRAB: "抓取种子",
    NotificationEvent.RADARR_RENAME: "文件重命名",
    NotificationEvent.RADARR_DELETE: "电影/文件删除",
    NotificationEvent.MEDIA_LIBRARY_DELETED: "媒体删除",
}

# 可选事件适用的服务器类型映射，用于单服通知页签精确筛选展示
OPTIONAL_EVENT_SERVER_TYPES: dict[NotificationEvent, tuple[str, ...]] = {
    NotificationEvent.SONARR_GRAB: ("sonarr",),
    NotificationEvent.SONARR_RENAME: ("sonarr",),
    NotificationEvent.SONARR_DELETE: ("sonarr",),
    NotificationEvent.RADARR_GRAB: ("radarr",),
    NotificationEvent.RADARR_RENAME: ("radarr",),
    NotificationEvent.RADARR_DELETE: ("radarr",),
    NotificationEvent.MEDIA_LIBRARY_DELETED: ("emby", "jellyfin"),
}

# 允许路由到管理员私聊组 (admin_dm_group) 的路由类别集合（全局与单服运维/审计均支持）
ADMIN_DM_ALLOWED_CATEGORIES: frozenset[NotificationRouteCategory] = frozenset(
    {
        NotificationRouteCategory.OPS_ALERT,
        NotificationRouteCategory.ACCOUNT_LIFECYCLE,
        NotificationRouteCategory.COMMUNITY_VERIFICATION,
        NotificationRouteCategory.COMMUNITY_LEAVE_AUDIT,
    }
)
