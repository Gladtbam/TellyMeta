from datetime import datetime

from pydantic import BaseModel

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
    registration_expiry_days: int | None = None
    registration_external_url: str | None = None
    registration_external_parser: str | None = None
    code_expiry_days: int = 30
    nsfw_enabled: bool | None = None
    allow_subtitle_upload: bool = True
    allow_request: bool = True
    allow_nfo: bool = True
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
    qb_username: str | None = None
    qb_torrent_limit: int = 43200
    qb_server_id: int | None = None


class ServerUpdate(BaseModel):
    """用于更新服务器的请求体"""

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
    registration_expiry_days: int | None = None
    registration_external_url: str | None = None
    registration_external_parser: str | None = None
    code_expiry_days: int | None = None
    nsfw_enabled: bool | None = None
    allow_subtitle_upload: bool | None = None
    allow_request: bool | None = None
    allow_nfo: bool | None = None
    tos: str | None = None

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
    allow_nfo: bool = True
    tos: str | None = None


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
    media_accounts: list[MediaAccountDto]
    available_servers: list[AvailableServerDto] = []


class ChangeScoreDto(BaseModel):
    """修改积分请求"""

    score_change: int


class AdminActionResponse(BaseModel):
    """管理员操作响应"""

    success: bool
    message: str
