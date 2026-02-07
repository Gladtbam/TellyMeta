from datetime import datetime

from pydantic import BaseModel

# --- 子模型 ---

class PathMapping(BaseModel):
    """路径映射模型"""
    remote: str # 远端/容器内路径
    local: str  # 本地/宿主机路径

class LibraryBinding(BaseModel):
    """媒体库绑定模型"""
    library_id: str
    library_name: str
    topic_id: int # 绑定的 Telegram 话题 ID

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
    type: str # sonarr / radarr

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
    server_id: int | None = None
    server_name: str | None = None
    arr_type: str | None = None
    quality_profile_id: int | None = None
    root_folder: str | None = None

class LibraryDto(BaseModel):
    """媒体库信息"""
    name: str
    id: str | None = None
    binding: BindingDto | None = None

class BindingUpdate(BaseModel):
    """更新绑定请求"""
    server_id: int
    quality_profile_id: int
    root_folder: str # 传递路径字符串

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
    tos: str | None = None

    # 高级配置
    path_mappings: list[PathMapping] = []
    library_bindings: list[LibraryBinding] = []

    class Config:
        from_attributes = True

class ServerCreate(BaseModel):
    """用于创建服务器的请求体"""
    name: str
    server_type: str
    url: str
    api_key: str

class ServerUpdate(BaseModel):
    """用于更新服务器的请求体"""
    name: str | None = None
    url: str | None = None
    api_key: str | None = None # 前端传 "****" 或 null 时忽略
    is_enabled: bool | None = None

    notify_topic_id: int | None = None
    request_notify_topic_id: int | None = None

    # Emby/Jellyfin
    registration_mode: str | None = None
    registration_count_limit: int | None = None
    registration_time_limit: str | None = None # 传递时间戳字符串
    registration_expiry_days: int | None = None
    registration_external_url: str | None = None
    registration_external_parser: str | None = None
    code_expiry_days: int | None = None
    nsfw_enabled: bool | None = None
    allow_subtitle_upload: bool | None = None
    tos: str | None = None

    # 高级配置
    path_mappings: list[PathMapping] = []
    library_bindings: list[LibraryBinding] = []

# --- System Models (系统配置相关) ---

class SystemConfigResponse(BaseModel):
    """系统开关状态响应"""
    enable_points: bool
    enable_verification: bool
    enable_requestmedia: bool
    enable_cleanup_inactive_users: bool

class ToggleResponse(BaseModel):
    """通用的开关操作响应"""
    success: bool
    key: str | None = None     # 前端请求的 key
    db_key: str | None = None  # 数据库真实的 key
    new_state: bool | None = None
    message: str | None = None

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

# --- Request Models (求片相关) ---

class RequestLibraryDto(BaseModel):
    """可求片的媒体库"""
    name: str
    type: str # sonarr / radarr

class MediaItemDto(BaseModel):
    """搜索结果单项"""
    media_id: int
    title: str
    year: int | str
    poster: str | None = None
    overview: str | None = None
    status: str = 'new' # new, existing, processing

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

class UserInfoDto(BaseModel):
    """用户信息聚合"""
    id: int
    score: int
    checkin_count: int
    warning_count: int
    renew_score: int
    is_admin: bool = False
    media_accounts: list[MediaAccountDto]
