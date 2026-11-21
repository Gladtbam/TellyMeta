"""Jellyfin Webhook 回调数据模型（基于 jellyfin-plugin-webhook 源码维护，兼容 Python 3.15 与 extra='allow'）。"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class NotificationType(StrEnum):
    """Jellyfin Webhook 支持的通知事件类型（对应上游 ``NotificationType``）。"""

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


class JellyfinWebhookModel(BaseModel):
    """Jellyfin Webhook 模型公共基类，确保所有主模型与 Mixin 均支持 ``extra='allow'``。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class BasePayload(JellyfinWebhookModel):
    """所有通知必然包含的基础字段（对应 ``DataObjectHelpers.GetBaseDataObject``）。"""

    server_id: str = Field(alias="ServerId")
    server_name: str = Field(alias="ServerName")
    server_version: str = Field(alias="ServerVersion")
    server_url: str = Field(alias="ServerUrl")
    # NotificationType 由具体的子类通过 Literal 定义，此处不声明以利用 Discriminator


class UserDataMixin(JellyfinWebhookModel):
    """对应 ``DataObjectHelpers.AddUserData``，包含用户基础信息。"""

    notification_username: str = Field(default="", alias="NotificationUsername")
    user_id: str = Field(default="", alias="UserId")
    last_login_date: datetime | None = Field(default=None, alias="LastLoginDate")
    last_activity_date: datetime | None = Field(default=None, alias="LastActivityDate")


class ItemInfoMixin(JellyfinWebhookModel):
    """对应 ``DataObjectHelpers.AddBaseItemData``；动态键 ``Provider_*`` 与流明细通过 ``extra='allow'`` 透传。"""

    timestamp: datetime | None = Field(default=None, alias="Timestamp")
    utc_timestamp: datetime | None = Field(default=None, alias="UtcTimestamp")
    item_id: str = Field(alias="ItemId")
    item_type: str = Field(alias="ItemType")
    name: str = Field(alias="Name")
    run_time_ticks: int | None = Field(default=None, alias="RunTimeTicks")
    run_time: str | None = Field(default=None, alias="RunTime")
    year: int | None = Field(default=None, alias="Year")
    premiere_date: str | None = Field(default=None, alias="PremiereDate")
    genres: str | None = Field(default=None, alias="Genres")
    overview: str | None = Field(default=None, alias="Overview")
    tagline: str | None = Field(default=None, alias="Tagline")
    aspect_ratio: str | None = Field(default=None, alias="AspectRatio")

    # 剧集 / 季专属信息
    series_name: str | None = Field(default=None, alias="SeriesName")
    series_id: str | None = Field(default=None, alias="SeriesId")
    season_id: str | None = Field(default=None, alias="SeasonId")
    series_premiere_date: str | None = Field(default=None, alias="SeriesPremiereDate")
    season_number: int | None = Field(default=None, alias="SeasonNumber")
    season_number_00: str | None = Field(default=None, alias="SeasonNumber00")
    season_number_000: str | None = Field(default=None, alias="SeasonNumber000")
    episode_number: int | None = Field(default=None, alias="EpisodeNumber")
    episode_number_00: str | None = Field(default=None, alias="EpisodeNumber00")
    episode_number_000: str | None = Field(default=None, alias="EpisodeNumber000")
    episode_number_end: int | None = Field(default=None, alias="EpisodeNumberEnd")
    episode_number_end_00: str | None = Field(default=None, alias="EpisodeNumberEnd00")
    episode_number_end_000: str | None = Field(
        default=None, alias="EpisodeNumberEnd000"
    )
    air_time: str | None = Field(default=None, alias="AirTime")

    # 音乐专属信息
    album: str | None = Field(default=None, alias="Album")
    artist: str | None = Field(default=None, alias="Artist")


class SessionInfoMixin(JellyfinWebhookModel):
    """对应 ``DataObjectHelpers.AddSessionInfoData``，注意仅 Session 非空时全量下发。"""

    device_name: str = Field(default="", alias="DeviceName")
    device_id: str | None = Field(default=None, alias="DeviceId")
    client_name: str | None = Field(default=None, alias="Client")
    remote_end_point: str | None = Field(default=None, alias="RemoteEndPoint")
    session_id: str | None = Field(default=None, alias="Id")
    last_playback_check_in: datetime | None = Field(
        default=None, alias="LastPlaybackCheckIn"
    )


class PlaybackInfoMixin(JellyfinWebhookModel):
    """对应 ``DataObjectHelpers.AddPlaybackProgressData``。"""

    playback_position_ticks: int = Field(alias="PlaybackPositionTicks")
    playback_position: str = Field(alias="PlaybackPosition")
    is_paused: bool = Field(alias="IsPaused")
    is_automated: bool | None = Field(default=None, alias="IsAutomated")
    # PlaybackProgress 与 SessionInfo 可能同时写入 DeviceId
    device_id: str | None = Field(default=None, alias="DeviceId")
    # 对应 AddPlaybackProgressData 写入的 ClientName
    client_name_pb: str | None = Field(default=None, alias="ClientName")
    play_method: str | None = Field(default=None, alias="PlayMethod")
    media_source_id: str | None = Field(default=None, alias="MediaSourceId")


class PluginInfoMixin(JellyfinWebhookModel):
    """对应 ``DataObjectHelpers.AddPluginInstallationInfo``。"""

    plugin_id: str = Field(alias="PluginId")
    plugin_name: str = Field(alias="PluginName")
    plugin_version: str = Field(alias="PluginVersion")
    plugin_changelog: str | None = Field(default=None, alias="PluginChangelog")
    plugin_source_url: str | None = Field(default=None, alias="PluginSourceUrl")
    plugin_checksum: str | None = Field(default=None, alias="PluginChecksum")


# --- 具体的事件模型定义 ---


class ItemEvent(BasePayload, ItemInfoMixin):
    """媒体库变更及字幕事件（ItemAdded / ItemDeleted / SubtitleDownloadFailure）。"""

    notification_type: Literal[
        NotificationType.ITEM_ADDED,
        NotificationType.ITEM_DELETED,
        NotificationType.SUBTITLE_DOWNLOAD_FAILURE,
    ] = Field(alias="NotificationType")


class PlaybackEvent(
    BasePayload, ItemInfoMixin, UserDataMixin, SessionInfoMixin, PlaybackInfoMixin
):
    """播放事件（PlaybackStart / PlaybackProgress / PlaybackStop）。"""

    notification_type: Literal[
        NotificationType.PLAYBACK_START,
        NotificationType.PLAYBACK_PROGRESS,
        NotificationType.PLAYBACK_STOP,
    ] = Field(alias="NotificationType")
    # 仅 PlaybackStop 携带
    played_to_completion: bool | None = Field(default=None, alias="PlayedToCompletion")


class UserEvent(BasePayload, UserDataMixin):
    """用户生命周期与密码事件。"""

    notification_type: Literal[
        NotificationType.USER_CREATED,
        NotificationType.USER_DELETED,
        NotificationType.USER_LOCKED_OUT,
        NotificationType.USER_PASSWORD_CHANGED,
        NotificationType.USER_UPDATED,
    ] = Field(alias="NotificationType")


class UserDataSavedEvent(BasePayload, ItemInfoMixin, UserDataMixin):
    """用户数据保存事件（对应 ``UserDataSavedNotifierEntryPoint``）。"""

    notification_type: Literal[NotificationType.USER_DATA_SAVED] = Field(
        alias="NotificationType"
    )
    save_reason: str | None = Field(default=None, alias="SaveReason")
    likes: bool | None = Field(default=None, alias="Likes")
    rating: float | None = Field(default=None, alias="Rating")
    playback_position_ticks: int | None = Field(
        default=None, alias="PlaybackPositionTicks"
    )
    playback_position: str | None = Field(default=None, alias="PlaybackPosition")
    play_count: int | None = Field(default=None, alias="PlayCount")
    is_favorite: bool | None = Field(default=None, alias="Favorite")
    played: bool | None = Field(default=None, alias="Played")
    audio_stream_index: int | None = Field(default=None, alias="AudioStreamIndex")
    subtitle_stream_index: int | None = Field(default=None, alias="SubtitleStreamIndex")
    last_played_date: datetime | None = Field(default=None, alias="LastPlayedDate")


class AuthenticationEvent(BasePayload, UserDataMixin, SessionInfoMixin):
    """认证成功/失败与会话启动事件（对应 Auth 及 SessionStart Notifier）。"""

    notification_type: Literal[
        NotificationType.AUTHENTICATION_SUCCESS,
        NotificationType.AUTHENTICATION_FAILURE,
        NotificationType.SESSION_START,
    ] = Field(alias="NotificationType")
    # AuthenticationFailure 会追加 App / Username / AppVersion / ClientName
    app: str | None = Field(default=None, alias="App")
    app_version: str | None = Field(default=None, alias="AppVersion")
    username: str | None = Field(default=None, alias="Username")
    client_name_pb: str | None = Field(default=None, alias="ClientName")


class PluginEvent(BasePayload, PluginInfoMixin):
    """插件安装、更新及异常事件。"""

    notification_type: Literal[
        NotificationType.PLUGIN_INSTALLED,
        NotificationType.PLUGIN_INSTALLING,
        NotificationType.PLUGIN_UPDATED,
        NotificationType.PLUGIN_INSTALLATION_FAILED,
        NotificationType.PLUGIN_INSTALLATION_CANCELLED,
    ] = Field(alias="NotificationType")
    # PluginInstallationFailed 通过 AddExceptionInfo 写入
    exception_message: str | None = Field(default=None, alias="ExceptionMessage")
    exception_message_inner: str | None = Field(
        default=None, alias="ExceptionMessageInner"
    )


class PluginUninstalledEvent(BasePayload):
    """插件卸载事件（``PluginUninstalledNotifier`` 使用独立字段集合）。"""

    notification_type: Literal[NotificationType.PLUGIN_UNINSTALLED] = Field(
        alias="NotificationType"
    )
    plugin_name: str = Field(alias="PluginName")
    plugin_version: str = Field(alias="PluginVersion")
    plugin_id: str = Field(alias="PluginId")
    plugin_description: str | None = Field(default=None, alias="PluginDescription")
    plugin_status: str | None = Field(default=None, alias="PluginStatus")


class SystemEvent(BasePayload):
    """系统事件（目前仅 PendingRestart）。"""

    notification_type: Literal[NotificationType.PENDING_RESTART] = Field(
        alias="NotificationType"
    )


class TaskEvent(BasePayload):
    """计划任务完成事件（对应 ``TaskCompletedNotifier``）。"""

    notification_type: Literal[NotificationType.TASK_COMPLETED] = Field(
        alias="NotificationType"
    )
    task_id: str = Field(alias="TaskId")
    task_name: str = Field(alias="TaskName")
    task_description: str | None = Field(default=None, alias="TaskDescription")
    task_category: str | None = Field(default=None, alias="TaskCategory")
    task_state: str = Field(alias="TaskState")
    result_id: str | None = Field(default=None, alias="ResultId")
    result_key: str | None = Field(default=None, alias="ResultKey")
    result_name: str | None = Field(default=None, alias="ResultName")
    result_status: str | None = Field(default=None, alias="ResultStatus")
    start_time: datetime | None = Field(default=None, alias="StartTime")
    end_time: datetime | None = Field(default=None, alias="EndTime")
    result_error_message: str | None = Field(default=None, alias="ResultErrorMessage")
    result_long_error_message: str | None = Field(
        default=None, alias="ResultLongErrorMessage"
    )


class GenericEvent(BasePayload):
    """自定义或测试类通用事件。"""

    notification_type: Literal[NotificationType.GENERIC] = Field(
        alias="NotificationType"
    )
    name: str | None = Field(default=None, alias="Name")
    description: str | None = Field(default=None, alias="Description")


# --- 联合类型（Union）与 Discriminator ---

JellyfinPayload = Annotated[
    ItemEvent
    | PlaybackEvent
    | UserEvent
    | UserDataSavedEvent
    | AuthenticationEvent
    | PluginEvent
    | PluginUninstalledEvent
    | SystemEvent
    | TaskEvent
    | GenericEvent,
    Field(discriminator="notification_type"),
]
