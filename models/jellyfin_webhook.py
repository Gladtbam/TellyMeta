from enum import StrEnum
from typing import Annotated, Literal
from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel, Field, ConfigDict

# 1. 枚举命名符合 Python 规范 (UPPER_CASE)
class NotificationType(StrEnum):
    ITEM_ADDED = "ItemAdded"
    ITEM_DELETED = "ItemDeleted"
    PLAYBACK_START = "PlaybackStart"
    PLAYBACK_PROGRESS = "PlaybackProgress"
    PLAYBACK_STOP = "PlaybackStop"
    SUBTITLE_DOWNLOAD_FAILURE = "SubtitleDownloadFailure"
    AUTHENTICATION_FAILURE = "AuthenticationFailure"
    AUTHENTICATION_SUCCESS = "AuthenticationSuccess"
    SESSION_START = "SessionStart"
    PENDING_RESTART = "PendingRestart"
    TASK_COMPLETED = "TaskCompleted"
    PLUGIN_INSTALLATION_CANCELLED = "PluginInstallationCancelled"
    PLUGIN_INSTALLATION_FAILED = "PluginInstallationFailed"
    PLUGIN_INSTALLED = "PluginInstalled"
    PLUGIN_INSTALLING = "PluginInstalling"
    PLUGIN_UNINSTALLED = "PluginUninstalled"
    PLUGIN_UPDATED = "PluginUpdated"
    USER_CREATED = "UserCreated"
    USER_DELETED = "UserDeleted"
    USER_LOCKED_OUT = "UserLockedOut"
    USER_PASSWORD_CHANGED = "UserPasswordChanged"
    USER_UPDATED = "UserUpdated"
    USER_DATA_SAVED = "UserDataSaved"
    GENERIC = "Generic"

# 2. 基础模型与 Mixins
# 使用 populate_by_name=True 允许通过 alias (PascalCase) 解析，但在 Python 中使用 snake_case

class BasePayload(BaseModel):
    """所有通知必然包含的基础字段"""
    model_config = ConfigDict(extra='allow', populate_by_name=True)

    server_id: str = Field(alias="ServerId")
    server_name: str = Field(alias="ServerName")
    server_version: str = Field(alias="ServerVersion")
    server_url: str = Field(alias="ServerUrl")
    # NotificationType 由具体的子类通过 Literal 定义，此处不声明以利用 Discriminator

class UserDataMixin(BaseModel):
    """对应 AddUserData，C# 中这些是必填的"""
    notification_username: str = Field(alias="NotificationUsername")
    user_id: str = Field(alias="UserId")
    last_login_date: datetime | None = Field(default=None, alias="LastLoginDate")
    last_activity_date: datetime | None = Field(default=None, alias="LastActivityDate")

class ItemInfoMixin(BaseModel):
    """对应 AddBaseItemData，核心字段必填，属性字段可选"""
    item_id: str = Field(alias="ItemId")
    item_type: str = Field(alias="ItemType")
    name: str = Field(alias="Name")

    # 以下字段在 C# 中是 Nullable 或条件判断的，因此保持 Optional
    run_time_ticks: int | None = Field(default=None, alias="RunTimeTicks")
    run_time: str | None = Field(default=None, alias="RunTime")
    year: int | None = Field(default=None, alias="Year")
    premiere_date: str | None = Field(default=None, alias="PremiereDate")
    genres: str | None = Field(default=None, alias="Genres")
    overview: str | None = Field(default=None, alias="Overview")
    tagline: str | None = Field(default=None, alias="Tagline")
    aspect_ratio: str | None = Field(default=None, alias="AspectRatio")

    # Series/Episode 特定字段
    series_name: str | None = Field(default=None, alias="SeriesName")
    series_id: str | None = Field(default=None, alias="SeriesId")
    season_id: str | None = Field(default=None, alias="SeasonId")
    season_number: int | None = Field(default=None, alias="SeasonNumber")
    season_number_00: str | None = Field(default=None, alias="SeasonNumber00")
    episode_number: int | None = Field(default=None, alias="EpisodeNumber")
    episode_number_00: str | None = Field(default=None, alias="EpisodeNumber00")

    # Music 特定字段
    album: str | None = Field(default=None, alias="Album")
    artist: str | None = Field(default=None, alias="Artist")

class SessionInfoMixin(BaseModel):
    """对应 AddSessionInfoData"""
    device_name: str = Field(alias="DeviceName")
    device_id: str | None = Field(default=None, alias="DeviceId")
    client_name: str | None = Field(default=None, alias="Client") # C# key is "Client"
    remote_end_point: str | None = Field(default=None, alias="RemoteEndPoint")
    session_id: str | None = Field(default=None, alias="Id")

class PlaybackInfoMixin(BaseModel):
    """对应 AddPlaybackProgressData"""
    playback_position_ticks: int = Field(alias="PlaybackPositionTicks")
    playback_position: str = Field(alias="PlaybackPosition")
    is_paused: bool = Field(alias="IsPaused")
    device_id: str | None = Field(default=None, alias="DeviceId") # 覆盖 Session 中的定义
    client_name_pb: str | None = Field(default=None, alias="ClientName") # C# key is "ClientName" here

    # 可选
    play_method: str | None = Field(default=None, alias="PlayMethod")
    media_source_id: str | None = Field(default=None, alias="MediaSourceId")

class PluginInfoMixin(BaseModel):
    """对应 AddPluginInstallationInfo"""
    plugin_id: str = Field(alias="PluginId")
    plugin_name: str = Field(alias="PluginName")
    plugin_version: str = Field(alias="PluginVersion")
    plugin_changelog: str | None = Field(default=None, alias="PluginChangelog")
    plugin_source_url: str | None = Field(default=None, alias="PluginSourceUrl")

# 3. 具体事件定义 (移除 | None，因为类型已确定)

class ItemEvent(BasePayload, ItemInfoMixin):
    """项目添加/删除"""
    notification_type: Literal[NotificationType.ITEM_ADDED, NotificationType.ITEM_DELETED] = Field(alias="NotificationType")

class PlaybackEvent(BasePayload, ItemInfoMixin, UserDataMixin, SessionInfoMixin, PlaybackInfoMixin):
    """播放相关"""
    notification_type: Literal[
        NotificationType.PLAYBACK_START,
        NotificationType.PLAYBACK_PROGRESS,
        NotificationType.PLAYBACK_STOP
    ] = Field(alias="NotificationType")

    # PlaybackStop 特有
    played_to_completion: bool | None = Field(default=None, alias="PlayedToCompletion")

class UserEvent(BasePayload, UserDataMixin):
    """用户账号变更"""
    notification_type: Literal[
        NotificationType.USER_CREATED,
        NotificationType.USER_DELETED,
        NotificationType.USER_LOCKED_OUT,
        NotificationType.USER_PASSWORD_CHANGED,
        NotificationType.USER_UPDATED
    ] = Field(alias="NotificationType")

class UserDataSavedEvent(BasePayload, ItemInfoMixin, UserDataMixin):
    """用户数据保存（如标记已播放、收藏）"""
    notification_type: Literal[NotificationType.USER_DATA_SAVED] = Field(alias="NotificationType")
    save_reason: str | None = Field(default=None, alias="SaveReason")
    is_favorite: bool | None = Field(default=None, alias="Favorite")
    played: bool | None = Field(default=None, alias="Played")
    rating: float | None = Field(default=None, alias="Rating")

class AuthenticationEvent(BasePayload, UserDataMixin, SessionInfoMixin):
    """认证事件"""
    notification_type: Literal[
        NotificationType.AUTHENTICATION_SUCCESS,
        NotificationType.AUTHENTICATION_FAILURE
    ] = Field(alias="NotificationType")

    # Auth Failure 特有
    app: str | None = Field(default=None, alias="App")
    app_version: str | None = Field(default=None, alias="AppVersion")

class PluginEvent(BasePayload, PluginInfoMixin):
    """插件操作"""
    notification_type: Literal[
        NotificationType.PLUGIN_INSTALLED,
        NotificationType.PLUGIN_INSTALLING,
        NotificationType.PLUGIN_UPDATED,
        NotificationType.PLUGIN_INSTALLATION_FAILED,
        NotificationType.PLUGIN_INSTALLATION_CANCELLED
    ] = Field(alias="NotificationType")

    exception_message: str | None = Field(default=None, alias="ExceptionMessage")

class PluginUninstalledEvent(BasePayload):
    """插件卸载 (字段与其他插件事件略有不同)"""
    notification_type: Literal[NotificationType.PLUGIN_UNINSTALLED] = Field(alias="NotificationType")
    plugin_name: str = Field(alias="PluginName")
    plugin_version: str = Field(alias="PluginVersion")
    plugin_id: str = Field(alias="PluginId")
    plugin_status: str | None = Field(default=None, alias="PluginStatus")

class SystemEvent(BasePayload):
    """系统级事件"""
    notification_type: Literal[NotificationType.PENDING_RESTART] = Field(alias="NotificationType")

class TaskEvent(BasePayload):
    """任务完成"""
    notification_type: Literal[NotificationType.TASK_COMPLETED] = Field(alias="NotificationType")
    task_id: str = Field(alias="TaskId")
    task_name: str = Field(alias="TaskName")
    task_state: str = Field(alias="TaskState")
    result_status: str | None = Field(default=None, alias="ResultStatus")
    start_time: datetime | None = Field(default=None, alias="StartTime")
    end_time: datetime | None = Field(default=None, alias="EndTime")

class GenericEvent(BasePayload):
    """兜底"""
    notification_type: Literal[NotificationType.GENERIC] = Field(alias="NotificationType")
    name: str | None = Field(default=None, alias="Name")
    description: str | None = Field(default=None, alias="Description")

# 4. 联合类型 (使用 | 语法)
# FastAPI 会根据 NotificationType 的值自动匹配到具体的类
JellyfinPayload = Annotated[
    ItemEvent |
    PlaybackEvent |
    UserEvent |
    UserDataSavedEvent |
    AuthenticationEvent |
    PluginEvent |
    PluginUninstalledEvent |
    SystemEvent |
    TaskEvent |
    GenericEvent,
    Field(discriminator="notification_type")
]
