import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.economy_policy import (
    BYTES_PER_GIB,
    DEFAULT_TRAFFIC_PACK_COST_SCORE,
    DEFAULT_TRAFFIC_PACK_SIZE_GIB,
)
from core.user_policy import DefaultUserPolicySpec
from models.events import (
    ADMIN_DM_ALLOWED_CATEGORIES,
    GLOBAL_ROUTE_CATEGORIES,
    SERVER_ROUTE_CATEGORIES,
    NotificationRouteCategory,
    NotificationRouteScope,
    NotificationTargetType,
    get_optional_events_for_category,
)
from repositories.config_repo import DEFAULT_TRAFFIC_PACK_UNIT_BYTES

DefaultUserPolicyDto = DefaultUserPolicySpec


def _validate_positive_pack_size_gib(value: float | None) -> float | None:
    """校验单包容量（GiB）大于 0 且换算字节数至少为 1 字节。"""
    if value is not None and (value <= 0 or round(value * BYTES_PER_GIB) < 1):
        raise ValueError("流量包单包容量（GiB）换算字节后必须大于 0")
    return value


# --- 子模型 ---


class PathMapping(BaseModel):
    """路径映射模型"""

    remote: str  # 远端/容器内路径
    local: str  # 本地/宿主机路径


class NsfwLibraryDto(BaseModel):
    """NSFW 媒体库状态"""

    id: str
    name: str
    is_nsfw: bool


# --- Binding Models (媒体库绑定相关) ---


class ArrServerDto(BaseModel):
    """下载器实例简略信息"""

    id: int
    name: str
    type: str  # sonarr / radarr


class QualityProfileDto(BaseModel):
    """质量配置信息"""

    id: int
    name: str | None = None


class RootFolderDto(BaseModel):
    """根目录信息"""

    id: int
    path: str
    freeSpace: int | None = None


class BindingDto(BaseModel):
    """绑定详情"""

    arr_id: int
    arr_name: str
    arr_type: str
    quality_profile_id: int
    root_folder: str


class LibraryDto(BaseModel):
    """媒体库信息"""

    name: str
    id: str | None = None
    binding: BindingDto | None = None


class BindingUpdate(BaseModel):
    """更新绑定请求"""

    arr_id: int
    quality_profile_id: int
    root_folder: str


# --- Server Models (服务器相关) ---


class ServerDto(BaseModel):
    """用于返回给前端的服务器信息（已脱敏）"""

    id: int
    name: str
    server_type: str
    url: str
    is_enabled: bool

    # 敏感字段脱敏处理
    api_key_masked: bool = True
    webhook_token: str | None = None

    # 基础配置
    notify_topic_id: int | None = None
    request_notify_topic_id: int | None = None

    # Emby/Jellyfin 特有配置
    registration_mode: str | None = None
    registration_count_limit: int = 0
    registration_time_limit: str = "0"
    registration_expiry_months: int = Field(default=1, ge=1)
    registration_external_url: str | None = None
    registration_external_parser: str | None = None
    code_expiry_days: int = 30
    nsfw_enabled: bool | None = None
    allow_subtitle_upload: bool = True
    allow_request: bool = False
    request_points_mode: str = "fixed"
    request_points_fixed: int = Field(default=0, ge=0)
    request_points_ratio: float = Field(default=0.0, ge=0.0)
    allow_nfo: bool = True
    enable_mkv_process: bool = True
    enable_llm_translate: bool = True
    enable_watch_time: bool = True
    enable_watch_time_renewal: bool = False
    watch_time_renewal_threshold_seconds: int = Field(default=0, ge=0)
    enable_traffic: bool = False
    enable_traffic_alert: bool = True
    enable_traffic_auto_suspend: bool = False
    traffic_quota_bytes: int = Field(default=0, ge=0)
    allow_traffic_pack: bool = False
    traffic_pack_points_mode: str = "fixed"
    traffic_pack_points_fixed: int = Field(default=50, ge=1)
    traffic_pack_points_ratio: float = Field(default=0.5, gt=0.0)
    traffic_pack_size_gib: float = Field(default=10.0, gt=0.0)
    default_user_policy: DefaultUserPolicyDto = Field(
        default_factory=DefaultUserPolicyDto
    )
    tos: str | None = None

    # 高级配置
    path_mappings: list[PathMapping] = []
    qb_username: str | None = None
    qb_torrent_limit: int = 43200
    qb_server_id: int | None = None

    class Config:
        from_attributes = True


class ServerCreate(BaseModel):
    """用于创建服务器的请求体"""

    name: str
    server_type: str
    url: str
    api_key: str
    registration_expiry_months: int = Field(default=1, ge=1)
    allow_request: bool = False
    request_points_mode: Literal["fixed", "ratio"] = "fixed"
    request_points_fixed: int = Field(default=0, ge=0)
    request_points_ratio: float = Field(default=0.0, ge=0.0)
    allow_nfo: bool = True
    enable_mkv_process: bool = True
    enable_llm_translate: bool = True
    enable_watch_time: bool = True
    enable_watch_time_renewal: bool = False
    watch_time_renewal_threshold_seconds: int = Field(default=0, ge=0)
    enable_traffic: bool = False
    enable_traffic_alert: bool = True
    enable_traffic_auto_suspend: bool = False
    traffic_quota_bytes: int = Field(default=0, ge=0)
    allow_traffic_pack: bool = False
    traffic_pack_points_mode: Literal["fixed", "ratio"] = "fixed"
    traffic_pack_points_fixed: int = Field(default=50, ge=1)
    traffic_pack_points_ratio: float = Field(default=0.5, gt=0.0)
    traffic_pack_size_gib: float = Field(default=10.0, gt=0.0)
    default_user_policy: DefaultUserPolicyDto | None = None
    qb_username: str | None = None
    qb_torrent_limit: int = 43200
    qb_server_id: int | None = None

    @field_validator("traffic_pack_size_gib")
    @classmethod
    def _check_pack_size_gib(cls, value: float) -> float:
        """校验单包容量 GiB 在换算字节后至少为 1 字节。"""
        checked = _validate_positive_pack_size_gib(value)
        assert checked is not None
        return checked


class ServerUpdate(BaseModel):
    """用于更新服务器的请求体"""

    # 显式拒绝已废弃的 registration_expiry_days 等未声明字段，防止静默忽略误导调用方
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    api_key: str | None = None  # 前端传 "****" 或 null 时忽略
    is_enabled: bool | None = None

    notify_topic_id: int | None = None
    request_notify_topic_id: int | None = None

    # Emby/Jellyfin
    registration_mode: str | None = None
    registration_count_limit: int | None = None
    registration_time_limit: str | None = None  # 传递时间戳字符串
    registration_expiry_months: int | None = Field(default=None, ge=1)
    registration_external_url: str | None = None
    registration_external_parser: str | None = None
    code_expiry_days: int | None = None
    nsfw_enabled: bool | None = None
    allow_subtitle_upload: bool | None = None
    allow_request: bool | None = None
    request_points_mode: Literal["fixed", "ratio"] | None = None
    request_points_fixed: int | None = Field(default=None, ge=0)
    request_points_ratio: float | None = Field(default=None, ge=0.0)
    allow_nfo: bool | None = None
    enable_mkv_process: bool | None = None
    enable_llm_translate: bool | None = None
    enable_watch_time: bool | None = None
    enable_watch_time_renewal: bool | None = None
    watch_time_renewal_threshold_seconds: int | None = Field(default=None, ge=0)
    enable_traffic: bool | None = None
    enable_traffic_alert: bool | None = None
    enable_traffic_auto_suspend: bool | None = None
    traffic_quota_bytes: int | None = Field(default=None, ge=0)
    allow_traffic_pack: bool | None = None
    traffic_pack_points_mode: Literal["fixed", "ratio"] | None = None
    traffic_pack_points_fixed: int | None = Field(default=None, ge=1)
    traffic_pack_points_ratio: float | None = Field(default=None, gt=0.0)
    traffic_pack_size_gib: float | None = Field(default=None, gt=0.0)
    default_user_policy: DefaultUserPolicyDto | None = None
    tos: str | None = None

    @field_validator("traffic_pack_size_gib")
    @classmethod
    def _check_pack_size_gib(cls, value: float | None) -> float | None:
        """校验单包容量 GiB 在换算字节后至少为 1 字节。"""
        return _validate_positive_pack_size_gib(value)

    # 高级配置
    path_mappings: list[PathMapping] = []
    qb_username: str | None = None
    qb_torrent_limit: int | None = None
    qb_server_id: int | None = None


# --- System Models (系统配置相关) ---


class SystemConfigResponse(BaseModel):
    """系统开关状态响应"""

    enable_points: bool
    enable_verification: bool
    enable_cleanup_inactive_users: bool
    enable_mkv_process: bool = True
    mkvtoolnix_available: bool = False
    mkv_exclude_keywords: str
    enable_llm_translate: bool = False
    ai_api_key_masked: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_temperature: float = 1.3
    ai_rpm: int | None = 60
    ai_rpd: int | None = None
    ai_tpm: int | None = None
    ai_concurrency: int = 1
    tmdb_api_key_masked: str = ""
    tvdb_api_key_masked: str = ""
    enable_points_checkin: bool = True
    enable_points_chat: bool = True
    enable_points_register: bool = True
    enable_points_renew: bool = True
    enable_points_request: bool = True
    enable_points_generate_code: bool = True
    enable_points_traffic_pack: bool = True
    enable_points_spam: bool = True
    enable_points_warn: bool = True
    effective_points_switches: dict[str, bool] = Field(default_factory=dict)
    points_renew_mode: str = "median"
    points_renew_fixed: int = 100
    points_renew_min: int = 100
    points_renew_max: int = 1000
    points_checkin_min: int = 1
    points_checkin_max: int = 3
    points_checkin_consecutive_bonus: int = 2
    points_checkin_lucky_interval: int = 7
    points_message_daily_cap: int = 20
    points_spam_threshold: int = 5
    verification_timeout_minutes: int = 5
    account_delete_grace_days: int = 7
    account_renew_advance_days: int = 7
    request_expire_days: int = 7
    schedule_ban_expired_day: str = "*"
    schedule_ban_expired_time: str = "00:15"
    schedule_delete_banned_day: str = "*"
    schedule_delete_banned_time: str = "00:30"
    schedule_cleanup_inactive_day: str = "*"
    schedule_cleanup_inactive_time: str = "01:00"
    schedule_settle_scores_day: str = "*"
    schedule_settle_scores_time: str = "23:00"
    schedule_cleanup_cache_day: str = "*"
    schedule_cleanup_cache_time: str = "03:00"
    schedule_backup_db_day: str = "*"
    schedule_backup_db_time: str = "04:00"
    schedule_request_expire_hours: int = 1
    schedule_nsfw_retry_minutes: int = 10
    db_backup_retention_days: int = 7
    enable_watch_time: bool = True
    watch_time_daily_cap_seconds: int = 43200
    watch_time_heartbeat_interval_seconds: int = 30
    watch_time_disconnect_grace_seconds: int = 45
    enable_traffic: bool = False
    traffic_udp_host: str = "0.0.0.0"
    traffic_udp_port: int = 5140
    traffic_flush_interval_seconds: int = 10


class OpsConfigUpdateDto(BaseModel):
    """定时任务调度与运维策略配置更新请求"""

    schedule_ban_expired_day: str = "*"
    schedule_ban_expired_time: str = "00:15"
    schedule_delete_banned_day: str = "*"
    schedule_delete_banned_time: str = "00:30"
    schedule_cleanup_inactive_day: str = "*"
    schedule_cleanup_inactive_time: str = "01:00"
    schedule_settle_scores_day: str = "*"
    schedule_settle_scores_time: str = "23:00"
    schedule_cleanup_cache_day: str = "*"
    schedule_cleanup_cache_time: str = "03:00"
    schedule_backup_db_day: str = "*"
    schedule_backup_db_time: str = "04:00"
    schedule_request_expire_hours: int = 1
    schedule_nsfw_retry_minutes: int = 10
    db_backup_retention_days: int = 7


class PointsConfigUpdateDto(BaseModel):
    """积分、签到与续费定价规则配置更新请求"""

    points_renew_mode: str = "median"
    points_renew_fixed: int = 100
    points_renew_min: int = 100
    points_renew_max: int = 1000
    points_checkin_min: int = 1
    points_checkin_max: int = 3
    points_checkin_consecutive_bonus: int = 2
    points_checkin_lucky_interval: int = 7
    points_message_daily_cap: int = 20
    points_spam_threshold: int = 5


class PolicyConfigUpdateDto(BaseModel):
    """验证超时与账号/求片宽限期配置更新请求"""

    verification_timeout_minutes: int = 5
    account_delete_grace_days: int = 7
    account_renew_advance_days: int = 7
    request_expire_days: int = 7


class WatchTimeConfigUpdateDto(BaseModel):
    """播放时长统计与 Emby 心跳参数更新请求"""

    daily_cap_seconds: int = 43200
    heartbeat_interval_seconds: int = 30
    disconnect_grace_seconds: int = 45


class TrafficConfigUpdateDto(BaseModel):
    """Nginx 流量 UDP 监听与内存聚合刷盘参数更新请求"""

    udp_host: str = "0.0.0.0"
    udp_port: int = 5140
    flush_interval_seconds: int = 10


class MkvConfigUpdateDto(BaseModel):
    """MKV 排除关键词配置更新请求"""

    mkv_exclude_keywords: str


class LlmConfigUpdateDto(BaseModel):
    """LLM 翻译参数配置更新请求"""

    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_temperature: float = 1.3
    ai_rpm: int | None = 60
    ai_rpd: int | None = None
    ai_tpm: int | None = None
    ai_concurrency: int = 1


class MetadataConfigUpdateDto(BaseModel):
    """TMDB / TVDB 元数据 API Key 更新请求"""

    tmdb_api_key: str = ""
    tvdb_api_key: str = ""


class ToggleResponse(BaseModel):
    """通用的开关操作响应"""

    success: bool
    key: str | None = None  # 前端请求的 key
    db_key: str | None = None  # 数据库真实的 key
    new_state: bool | None = None
    message: str | None = None
    dm_delivered: bool | None = None
    one_time_result: str | None = None


# --- Admin & Topic Models (管理员与话题) ---


class AdminDto(BaseModel):
    """管理员信息"""

    id: int
    name: str
    username: str | None = None
    is_bot_admin: bool


class TopicDto(BaseModel):
    """群组话题信息"""

    id: int
    name: str


class TopicsResponse(BaseModel):
    """群组话题响应"""

    is_forum: bool
    topics: list[TopicDto]


# --- Request Models (求片相关) ---


class RequestLibraryDto(BaseModel):
    """可求片的媒体库"""

    name: str
    type: str  # sonarr / radarr


class MediaItemDto(BaseModel):
    """搜索结果单项"""

    media_id: int
    title: str
    year: int | str
    poster: str | None = None
    overview: str | None = None
    status: str = "new"  # new, existing, processing


class RequestSubmitDto(BaseModel):
    """提交求片请求"""

    library_name: str
    media_id: int


# --- User Models (用户相关) ---


class AutoRenewToggleDto(BaseModel):
    """媒体账号自动续期开关切换请求体"""

    enabled: bool | None = None


class MediaAccountDto(BaseModel):
    """媒体账户信息"""

    server_id: int
    media_name: str
    server_name: str
    server_type: str
    server_url: str
    status_text: str
    expires_at: datetime
    is_banned: bool
    allow_subtitle_upload: bool = True
    allow_request: bool = True
    request_open: bool = False
    request_points_mode: str = "fixed"
    request_points_cost: int = 0
    tos: str | None = None
    allow_nfo: bool = True
    enable_watch_time: bool = True
    cycle_watch_time_seconds: int = 0
    total_watch_time_seconds: int = 0
    anchor_day: int | None = None
    next_reset_at: datetime | None = None
    auto_renew: bool = False
    auto_renew_open: bool = False
    enable_watch_time_renewal: bool = False
    watch_time_renewal_threshold_seconds: int = 0
    watch_time_renewal_open: bool = False
    watch_time_renewal_eligible: bool = False
    watch_time_renewal_status_text: str = "当前服务器未开放保活续期"
    watch_time_progress_percent: float | None = None
    enable_traffic: bool = False
    traffic_limited: bool = False
    cycle_traffic_bytes: int = 0
    traffic_pack_bytes: int = 0
    total_traffic_bytes: int = 0
    server_traffic_quota_bytes: int = 0
    cycle_traffic_adjustment_bytes: int = 0
    traffic_quota_bytes: int = 0
    traffic_remaining_bytes: int | None = None
    traffic_total_remaining_bytes: int | None = None
    is_traffic_suspended: bool = False
    enable_traffic_auto_suspend: bool = False
    allow_traffic_pack: bool = False
    traffic_pack_open: bool = False
    traffic_pack_points_mode: str = "fixed"
    traffic_pack_cost_score: int = DEFAULT_TRAFFIC_PACK_COST_SCORE
    traffic_pack_size_gib: float = DEFAULT_TRAFFIC_PACK_SIZE_GIB
    traffic_pack_unit_bytes: int = DEFAULT_TRAFFIC_PACK_UNIT_BYTES
    can_points_renew: bool = True
    can_generate_code: bool = True


class AvailableServerDto(BaseModel):
    """可注册的服务器信息（用户尚未注册的）"""

    server_id: int
    server_name: str
    server_type: str
    server_url: str
    registration_mode: str
    can_register: bool = False
    reason: str = ""
    status_label: str = ""
    tos: str | None = None
    has_external_verification: bool = False
    requires_points: bool = False
    points_cost: int = 0


class UserInfoDto(BaseModel):
    """用户信息聚合"""

    id: int
    username: str | None = None
    full_name: str | None = None
    score: int
    checkin_count: int
    consecutive_checkin_count: int = 0
    warning_count: int
    renew_score: int
    is_admin: bool = False
    is_group_member: bool = False
    has_username: bool = False
    cycle_watch_time_seconds: int = 0
    total_watch_time_seconds: int = 0
    enable_points: bool = True
    enable_points_checkin: bool = True
    enable_points_chat: bool = True
    enable_points_register: bool = True
    enable_points_renew: bool = True
    enable_points_request: bool = True
    enable_points_generate_code: bool = True
    enable_points_traffic_pack: bool = True
    enable_points_spam: bool = True
    enable_points_warn: bool = True
    enable_watch_time: bool = True
    enable_traffic: bool = False
    unread_notification_count: int = 0
    has_unread_mandatory_notification: bool = False
    media_accounts: list[MediaAccountDto]
    available_servers: list[AvailableServerDto] = []


class TrafficPackRedeemDto(BaseModel):
    """Mini App 积分兑换流量包请求体"""

    quantity: int = Field(default=1, ge=1, le=100)


class TrafficPackRedeemResponse(BaseModel):
    """Mini App 积分兑换流量包响应"""

    success: bool
    message: str
    added_bytes: int = 0
    cost_score: int = 0
    remaining_score: int | None = None
    traffic_pack_bytes: int = 0
    is_traffic_suspended: bool = False
    unsuspended: bool = False


class ChangeScoreDto(BaseModel):
    """修改积分请求"""

    score_change: int


class AdminTrafficAdjustDto(BaseModel):
    """管理员单账号当期流量临时调额与当期已用清零请求"""

    delta_gib: float = 0.0
    reset_cycle_usage: bool = False

    @field_validator("delta_gib")
    @classmethod
    def _validate_delta_finite(cls, value: float) -> float:
        """校验调额增减量必须为有限数值。"""
        if not math.isfinite(value):
            raise ValueError("调额 GiB 数值必须为有限实数")
        return value

    @model_validator(mode="after")
    def _validate_non_noop(self) -> AdminTrafficAdjustDto:
        """防止既不清零当期已用流量、折算字节增减又为 0 的空提交。"""
        if not self.reset_cycle_usage and round(self.delta_gib * BYTES_PER_GIB) == 0:
            raise ValueError("请指定非零的临时调额 GiB 或勾选清零当期已用流量")
        return self


class AdminActionResponse(BaseModel):
    """管理员操作响应"""

    success: bool
    message: str
    effective_quota_bytes: int | None = None
    cycle_traffic_bytes: int | None = None
    cycle_traffic_adjustment_bytes: int | None = None
    traffic_remaining_bytes: int | None = None
    traffic_total_remaining_bytes: int | None = None
    is_traffic_suspended: bool | None = None
    unsuspended: bool = False


class DefaultUserPolicySyncResponse(BaseModel):
    """管理员将单服默认 UserPolicy 同步至存量媒体账号的响应"""

    success: bool
    message: str
    total_count: int = 0
    synced_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


# --- Notification Routing, Inbox, Preferences & Ops Logs ---


def _validate_route_target_params(
    *,
    scope_type: NotificationRouteScope,
    server_id: int | None,
    category: NotificationRouteCategory,
    target_type: NotificationTargetType,
    target_chat_id: int | None,
    target_topic_id: int | None,
) -> tuple[int | None, int | None]:
    """统一校验纯 Telegram 通知路由目标参数合法性并返回规范化后的 (chat_id, topic_id)。"""
    if scope_type == NotificationRouteScope.GLOBAL:
        if server_id is not None:
            raise ValueError("全局路由规则不允许指定 server_id")
        if category not in GLOBAL_ROUTE_CATEGORIES:
            raise ValueError(
                f"类别 {category.value} 不设全局总开关，请在对应服务器「通知」页签独立配置"
            )
    if scope_type == NotificationRouteScope.SERVER:
        if server_id is None or server_id <= 0:
            raise ValueError("单服通知路由规则必须提供有效的 server_id")
        if category not in SERVER_ROUTE_CATEGORIES:
            raise ValueError(f"类别 {category.value} 仅支持全局路由配置")

    if target_type == NotificationTargetType.DISABLED:
        return None, None
    if target_type == NotificationTargetType.MAIN_CHAT:
        return None, None
    if target_type == NotificationTargetType.FORUM_TOPIC:
        if target_topic_id is None or target_topic_id <= 0:
            raise ValueError("话题路由 (forum_topic) 必须提供正整数 target_topic_id")
        if target_chat_id is not None and target_chat_id >= 0:
            raise ValueError(
                "指定话题所在群组时，target_chat_id 必须为负数群组 ID（留空表示使用主群）"
            )
        return target_chat_id, int(target_topic_id)
    if target_type == NotificationTargetType.CHAT_OR_CHANNEL:
        if target_chat_id is None or target_chat_id >= 0:
            raise ValueError(
                "独立群组/频道路由 (chat_or_channel) 必须提供负数 target_chat_id (< 0)"
            )
        return int(target_chat_id), None
    if target_type == NotificationTargetType.ADMIN_DM_GROUP:
        if category not in ADMIN_DM_ALLOWED_CATEGORIES:
            raise ValueError(
                "admin_dm_group 仅适用于运维告警、账号审计与群组治理审计路由类别"
            )
        return None, None
    raise ValueError(f"不支持的目标类型: {target_type}")


class OptionalEventOptionDto(BaseModel):
    """路由类别下默认关闭的可选事件描述"""

    event: str
    label: str
    severity: str
    server_types: list[str] = Field(default_factory=list)


class RouteCategoryLabelOverrideDto(BaseModel):
    """按服务器类型定制的路由类别展示标题与说明"""

    label: str
    description: str


class RouteCategoryMetaDto(BaseModel):
    """通知路由类别元数据描述"""

    category: str
    label: str
    description: str
    allowed_target_types: list[str]
    supports_escalate_admin_dm: bool = False
    inherits_global: bool = False
    optional_events: list[OptionalEventOptionDto] = Field(default_factory=list)
    server_type_labels: dict[str, RouteCategoryLabelOverrideDto] = Field(
        default_factory=dict
    )


class NotificationRouteDto(BaseModel):
    """单条通知路由规则 DTO"""

    id: int | None = None
    scope_type: Literal["global", "server"]
    server_id: int | None = None
    category: str
    target_type: str
    target_chat_id: int | None = None
    target_topic_id: int | None = None
    escalate_admin_dm: bool = False
    enabled_events: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class NotificationRouteUpsertDto(BaseModel):
    """新增或更新全局/单服通知路由规则请求体"""

    model_config = ConfigDict(extra="forbid")

    scope_type: NotificationRouteScope
    server_id: int | None = None
    category: NotificationRouteCategory
    target_type: NotificationTargetType
    target_chat_id: int | None = None
    target_topic_id: int | None = None
    escalate_admin_dm: bool = False
    enabled_events: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_route(self) -> NotificationRouteUpsertDto:
        """校验目标组合、可选事件白名单及高危抄送作用域约束。"""
        chat_id, topic_id = _validate_route_target_params(
            scope_type=self.scope_type,
            server_id=self.server_id,
            category=self.category,
            target_type=self.target_type,
            target_chat_id=self.target_chat_id,
            target_topic_id=self.target_topic_id,
        )
        self.target_chat_id = chat_id
        self.target_topic_id = topic_id

        allowed_events = {
            ev.value for ev in get_optional_events_for_category(self.category)
        }
        cleaned_events: list[str] = []
        seen: set[str] = set()
        for raw_event in self.enabled_events:
            ev_name = str(raw_event).strip()
            if not ev_name:
                continue
            if ev_name not in allowed_events:
                raise ValueError(
                    f"类别 {self.category.value} 不支持可选事件: {ev_name}"
                )
            if ev_name not in seen:
                seen.add(ev_name)
                cleaned_events.append(ev_name)
        self.enabled_events = sorted(cleaned_events)

        if self.category != NotificationRouteCategory.OPS_ALERT:
            self.escalate_admin_dm = False
        return self


class ServerCategoryRouteStateDto(BaseModel):
    """单台服务器在指定类别下的有效解析态与来源详情"""

    category: str
    source: Literal["server_override", "global", "legacy_topic", "default_disabled"]
    is_override: bool
    effective: NotificationRouteDto
    override: NotificationRouteDto | None = None
    legacy_topic_id: int | None = None


class ServerRouteOverviewDto(BaseModel):
    """单台服务器的通知路由总览"""

    server_id: int
    server_name: str
    server_type: str
    notify_topic_id: int | None = None
    request_notify_topic_id: int | None = None
    categories: dict[str, ServerCategoryRouteStateDto]


class NotificationRoutesResponse(BaseModel):
    """管理后台通知路由矩阵全量响应"""

    global_routes: list[NotificationRouteDto]
    server_routes: list[NotificationRouteDto]
    servers: list[ServerRouteOverviewDto]
    categories: list[RouteCategoryMetaDto]
    server_categories: list[RouteCategoryMetaDto] = Field(default_factory=list)
    optional_events: dict[str, list[OptionalEventOptionDto]]
    telegram_chat_id_configured: bool
    telegram_chat_id: int | None = None


class NotificationRouteMutationResponse(BaseModel):
    """通知路由保存响应"""

    success: bool
    message: str
    route: NotificationRouteDto


class NotificationRouteDeleteResponse(BaseModel):
    """删除单服覆盖路由响应"""

    success: bool
    deleted: bool
    message: str


class NotificationRouteTestRequest(BaseModel):
    """触发 SYSTEM_TEST 通路实测请求体（支持测试已保存或候选路由参数）"""

    model_config = ConfigDict(extra="forbid")

    category: NotificationRouteCategory
    server_id: int | None = None
    target_type: NotificationTargetType | None = None
    target_chat_id: int | None = None
    target_topic_id: int | None = None
    escalate_admin_dm: bool = False
    message: str = Field(
        default="这是一条系统通知通路测试消息。",
        max_length=500,
    )

    @model_validator(mode="after")
    def _validate_candidate_target(self) -> NotificationRouteTestRequest:
        """校验测试路由的作用域约束，并在传入候选目标参数时复用路由目标规则严格校验。"""
        if self.server_id is not None and self.server_id <= 0:
            raise ValueError("server_id 必须为正整数")
        if self.server_id is None and self.category not in GLOBAL_ROUTE_CATEGORIES:
            raise ValueError(
                f"类别 {self.category.value} 不设全局总开关，测试时请指定 server_id"
            )
        if self.server_id is not None and self.category not in SERVER_ROUTE_CATEGORIES:
            raise ValueError(f"类别 {self.category.value} 仅支持全局路由测试")
        if self.target_type is not None:
            scope_type = (
                NotificationRouteScope.SERVER
                if self.server_id is not None
                else NotificationRouteScope.GLOBAL
            )
            chat_id, topic_id = _validate_route_target_params(
                scope_type=scope_type,
                server_id=self.server_id,
                category=self.category,
                target_type=self.target_type,
                target_chat_id=self.target_chat_id,
                target_topic_id=self.target_topic_id,
            )
            self.target_chat_id = chat_id
            self.target_topic_id = topic_id
        if self.category != NotificationRouteCategory.OPS_ALERT:
            self.escalate_admin_dm = False
        return self


class NotificationRouteTestResponse(BaseModel):
    """SYSTEM_TEST 通路实测响应"""

    success: bool
    message: str
    delivered_targets: list[str] = Field(default_factory=list)
    error: str | None = None


class OpsAlertLogDto(BaseModel):
    """近 30 天系统运维告警日志条目 DTO"""

    id: int
    source_key: str
    server_id: int | None = None
    server_name: str | None = None
    event_type: str
    severity: str
    title: str
    body_text: str
    delivered_targets: list[str] = Field(default_factory=list)
    delivery_status: str
    error_message: str | None = None
    created_at: datetime


class OpsAlertLogsPageResponse(BaseModel):
    """分页运维告警日志响应"""

    items: list[OpsAlertLogDto]
    total: int
    limit: int
    offset: int


class NotificationInboxItemDto(BaseModel):
    """Mini App 站内兜底收件箱单条通知 DTO"""

    id: int
    server_id: int | None = None
    server_name: str | None = None
    event_type: str
    category: str
    severity: str
    title: str
    body_text: str
    action_url: str | None = None
    is_mandatory: bool = False
    is_read: bool = False
    delivery_error: str | None = None
    created_at: datetime
    read_at: datetime | None = None


class UserNotificationPreferencesDto(BaseModel):
    """用户 5 项可选提醒偏好开关视图"""

    allow_watch_time_milestone: bool = True
    allow_pre_expiry_reminder: bool = True
    allow_traffic_80_alert: bool = True
    allow_cycle_reset: bool = False
    allow_self_action_receipt: bool = True


class UserNotificationPreferencesUpdateDto(BaseModel):
    """更新用户可选提醒偏好开关请求体（严禁未知字段或篡改必达开关）"""

    model_config = ConfigDict(extra="forbid")

    allow_watch_time_milestone: bool | None = None
    allow_pre_expiry_reminder: bool | None = None
    allow_traffic_80_alert: bool | None = None
    allow_cycle_reset: bool | None = None
    allow_self_action_receipt: bool | None = None


class UserNotificationPreferencesResponse(BaseModel):
    """读写用户通知偏好响应"""

    success: bool = True
    allow_watch_time_milestone: bool
    allow_pre_expiry_reminder: bool
    allow_traffic_80_alert: bool
    allow_cycle_reset: bool
    allow_self_action_receipt: bool
    mandatory_notice_locked: bool = True


class MiniAppNotificationsResponse(BaseModel):
    """Mini App 通知中心聚合响应（近 30 天兜底收件箱、偏好与管理员运维日志）"""

    items: list[NotificationInboxItemDto]
    unread_count: int
    has_unread_mandatory: bool
    preferences: UserNotificationPreferencesDto
    is_admin: bool = False
    ops_logs: list[OpsAlertLogDto] = Field(default_factory=list)


class NotificationMarkReadDto(BaseModel):
    """Mini App 标记站内收件箱已读请求体"""

    model_config = ConfigDict(extra="forbid")

    item_id: int | None = None
    mark_all: bool = False

    @model_validator(mode="after")
    def _validate_target(self) -> NotificationMarkReadDto:
        """必须指定正整数 item_id 或 mark_all=True，防止无意义空请求。"""
        if self.mark_all:
            return self
        if self.item_id is None or self.item_id <= 0:
            raise ValueError("请指定有效的 item_id 或设置 mark_all=true")
        return self


class NotificationMarkReadResponse(BaseModel):
    """标记站内收件箱已读响应"""

    success: bool
    marked_count: int
    unread_count: int
    has_unread_mandatory: bool
