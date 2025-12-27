"""
Emby Webhook Pydantic Models

基于 Emby.Webhooks.dll (v1.0.37.0) 和 MediaBrowser.Controller (v4.9.1.90) 反编译生成的 Pydantic 模型。
用于解析 Emby Server 通过 Webhook 插件发送的 JSON 通知数据。

使用 Literal + Discriminated Union 实现类型安全的事件处理。
每个事件类型有自己的模型，字段根据实际情况定义为必需或可选。

Python 3.11+ 兼容
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# 枚举类型 (Enums)
# ============================================================================


class LogSeverity(StrEnum):
    """日志严重程度 (MediaBrowser.Model.Logging.LogSeverity)"""

    INFO = "Info"
    DEBUG = "Debug"
    WARN = "Warn"
    ERROR = "Error"
    FATAL = "Fatal"


class RecordingStatus(StrEnum):
    """录制状态 (MediaBrowser.Model.LiveTv.RecordingStatus)"""

    NEW = "New"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    CONFLICTED_OK = "ConflictedOk"
    CONFLICTED_NOT_OK = "ConflictedNotOk"
    ERROR = "Error"


class ImageType(StrEnum):
    """图像类型 (MediaBrowser.Model.Entities.ImageType)"""

    PRIMARY = "Primary"
    ART = "Art"
    BACKDROP = "Backdrop"
    BANNER = "Banner"
    LOGO = "Logo"
    THUMB = "Thumb"
    DISC = "Disc"
    BOX = "Box"
    SCREENSHOT = "Screenshot"
    MENU = "Menu"
    CHAPTER = "Chapter"
    BOX_REAR = "BoxRear"
    THUMBNAIL = "Thumbnail"
    LOGO_LIGHT = "LogoLight"
    LOGO_LIGHT_COLOR = "LogoLightColor"


class TimerType(StrEnum):
    """定时器类型 (MediaBrowser.Model.LiveTv.TimerType)"""

    PROGRAM = "Program"
    DATE_TIME = "DateTime"
    KEYWORD = "Keyword"


class KeepUntil(StrEnum):
    """保留直到 (MediaBrowser.Model.LiveTv.KeepUntil)"""

    UNTIL_DELETED = "UntilDeleted"
    UNTIL_SPACE_NEEDED = "UntilSpaceNeeded"
    UNTIL_WATCHED = "UntilWatched"
    UNTIL_DATE = "UntilDate"


class PackageVersionClass(StrEnum):
    """包版本类型 (MediaBrowser.Model.Updates.PackageVersionClass)"""

    RELEASE = "Release"
    BETA = "Beta"
    DEV = "Dev"


class MediaStreamType(StrEnum):
    """媒体流类型 (MediaBrowser.Model.Entities.MediaStreamType)"""

    UNKNOWN = "Unknown"
    AUDIO = "Audio"
    VIDEO = "Video"
    SUBTITLE = "Subtitle"
    EMBEDDED_IMAGE = "EmbeddedImage"
    ATTACHMENT = "Attachment"
    DATA = "Data"


class MediaProtocol(StrEnum):
    """媒体协议 (MediaBrowser.Model.MediaInfo.MediaProtocol)"""

    FILE = "File"
    HTTP = "Http"
    RTMP = "Rtmp"
    RTSP = "Rtsp"
    UDP = "Udp"
    RTP = "Rtp"
    FTP = "Ftp"
    MMS = "Mms"


class Video3DFormat(StrEnum):
    """3D视频格式 (MediaBrowser.Model.Entities.Video3DFormat)"""

    HALF_SIDE_BY_SIDE = "HalfSideBySide"
    FULL_SIDE_BY_SIDE = "FullSideBySide"
    FULL_TOP_AND_BOTTOM = "FullTopAndBottom"
    HALF_TOP_AND_BOTTOM = "HalfTopAndBottom"
    MVC = "MVC"


class LocationType(StrEnum):
    """位置类型 (MediaBrowser.Model.Entities.LocationType)"""

    FILE_SYSTEM = "FileSystem"
    VIRTUAL = "Virtual"


class MediaSourceType(StrEnum):
    """媒体源类型 (MediaBrowser.Model.Dto.MediaSourceType)"""

    DEFAULT = "Default"
    GROUPING = "Grouping"
    PLACEHOLDER = "Placeholder"


class TranscodeReason(StrEnum):
    """转码原因 (MediaBrowser.Model.Session.TranscodeReason)"""

    CONTAINER_NOT_SUPPORTED = "ContainerNotSupported"
    VIDEO_CODEC_NOT_SUPPORTED = "VideoCodecNotSupported"
    AUDIO_CODEC_NOT_SUPPORTED = "AudioCodecNotSupported"
    CONTAINER_BITRATE_EXCEEDS_LIMIT = "ContainerBitrateExceedsLimit"
    AUDIO_BITRATE_NOT_SUPPORTED = "AudioBitrateNotSupported"
    AUDIO_CHANNELS_NOT_SUPPORTED = "AudioChannelsNotSupported"
    VIDEO_RESOLUTION_NOT_SUPPORTED = "VideoResolutionNotSupported"
    UNKNOWN_VIDEO_STREAM_INFO = "UnknownVideoStreamInfo"
    UNKNOWN_AUDIO_STREAM_INFO = "UnknownAudioStreamInfo"
    AUDIO_PROFILE_NOT_SUPPORTED = "AudioProfileNotSupported"
    AUDIO_SAMPLE_RATE_NOT_SUPPORTED = "AudioSampleRateNotSupported"
    ANAMORPHIC_VIDEO_NOT_SUPPORTED = "AnamorphicVideoNotSupported"
    INTERLACED_VIDEO_NOT_SUPPORTED = "InterlacedVideoNotSupported"
    SECONDARY_AUDIO_NOT_SUPPORTED = "SecondaryAudioNotSupported"
    REF_FRAMES_NOT_SUPPORTED = "RefFramesNotSupported"
    VIDEO_BIT_DEPTH_NOT_SUPPORTED = "VideoBitDepthNotSupported"
    VIDEO_BITRATE_NOT_SUPPORTED = "VideoBitrateNotSupported"
    VIDEO_FRAMERATE_NOT_SUPPORTED = "VideoFramerateNotSupported"
    VIDEO_LEVEL_NOT_SUPPORTED = "VideoLevelNotSupported"
    VIDEO_PROFILE_NOT_SUPPORTED = "VideoProfileNotSupported"
    AUDIO_BIT_DEPTH_NOT_SUPPORTED = "AudioBitDepthNotSupported"
    SUBTITLE_CODEC_NOT_SUPPORTED = "SubtitleCodecNotSupported"
    DIRECT_PLAY_ERROR = "DirectPlayError"
    VIDEO_RANGE_NOT_SUPPORTED = "VideoRangeNotSupported"


class DayOfWeek(IntEnum):
    """星期几"""

    SUNDAY = 0
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6


# ============================================================================
# 基础模型配置
# ============================================================================


class EmbyBaseModel(BaseModel):
    """Emby 基础模型，配置通用选项"""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="allow",
        str_strip_whitespace=True,
    )


# ============================================================================
# 通知相关模型 (Notification Models)
# ============================================================================


class NotificationUserDto(EmbyBaseModel):
    """通知用户信息"""

    name: str = Field(alias="Name")
    id: str = Field(alias="Id")


class NotificationServerInfo(EmbyBaseModel):
    """通知服务器信息"""

    name: str = Field(alias="Name")
    id: str = Field(alias="Id")
    version: str = Field(alias="Version")


class NotificationSessionInfo(EmbyBaseModel):
    """通知会话信息"""

    remote_end_point: str | None = Field(default=None, alias="RemoteEndPoint")
    client: str | None = Field(default=None, alias="Client")
    device_name: str | None = Field(default=None, alias="DeviceName")
    device_id: str | None = Field(default=None, alias="DeviceId")
    application_version: str | None = Field(default=None, alias="ApplicationVersion")
    id: str | None = Field(default=None, alias="Id")


class NotificationRecordingInfo(EmbyBaseModel):
    """通知录制信息"""

    path: str | None = Field(default=None, alias="Path")
    status: RecordingStatus | None = Field(default=None, alias="Status")


# ============================================================================
# 媒体流模型 (Media Stream Models)
# ============================================================================


class MediaStream(EmbyBaseModel):
    """媒体流信息"""

    codec: str | None = Field(default=None, alias="Codec")
    codec_tag: str | None = Field(default=None, alias="CodecTag")
    language: str | None = Field(default=None, alias="Language")
    color_transfer: str | None = Field(default=None, alias="ColorTransfer")
    color_primaries: str | None = Field(default=None, alias="ColorPrimaries")
    color_space: str | None = Field(default=None, alias="ColorSpace")
    title: str | None = Field(default=None, alias="Title")
    display_title: str | None = Field(default=None, alias="DisplayTitle")
    is_interlaced: bool = Field(default=False, alias="IsInterlaced")
    channel_layout: str | None = Field(default=None, alias="ChannelLayout")
    bit_rate: int | None = Field(default=None, alias="BitRate")
    bit_depth: int | None = Field(default=None, alias="BitDepth")
    channels: int | None = Field(default=None, alias="Channels")
    sample_rate: int | None = Field(default=None, alias="SampleRate")
    is_default: bool = Field(default=False, alias="IsDefault")
    is_forced: bool = Field(default=False, alias="IsForced")
    height: int | None = Field(default=None, alias="Height")
    width: int | None = Field(default=None, alias="Width")
    average_frame_rate: float | None = Field(default=None, alias="AverageFrameRate")
    real_frame_rate: float | None = Field(default=None, alias="RealFrameRate")
    profile: str | None = Field(default=None, alias="Profile")
    type: MediaStreamType | None = Field(default=None, alias="Type")
    aspect_ratio: str | None = Field(default=None, alias="AspectRatio")
    index: int = Field(default=0, alias="Index")
    is_external: bool = Field(default=False, alias="IsExternal")
    path: str | None = Field(default=None, alias="Path")
    pixel_format: str | None = Field(default=None, alias="PixelFormat")
    level: float | None = Field(default=None, alias="Level")


class ChapterInfo(EmbyBaseModel):
    """章节信息"""

    start_position_ticks: int = Field(default=0, alias="StartPositionTicks")
    name: str | None = Field(default=None, alias="Name")
    image_path: str | None = Field(default=None, alias="ImagePath")
    image_tag: str | None = Field(default=None, alias="ImageTag")


class MediaSourceInfo(EmbyBaseModel):
    """媒体源信息"""

    protocol: MediaProtocol | None = Field(default=None, alias="Protocol")
    id: str | None = Field(default=None, alias="Id")
    path: str | None = Field(default=None, alias="Path")
    type: MediaSourceType | None = Field(default=None, alias="Type")
    container: str | None = Field(default=None, alias="Container")
    size: int | None = Field(default=None, alias="Size")
    name: str | None = Field(default=None, alias="Name")
    run_time_ticks: int | None = Field(default=None, alias="RunTimeTicks")
    supports_transcoding: bool = Field(default=True, alias="SupportsTranscoding")
    supports_direct_stream: bool = Field(default=True, alias="SupportsDirectStream")
    supports_direct_play: bool = Field(default=True, alias="SupportsDirectPlay")
    is_remote: bool = Field(default=False, alias="IsRemote")
    video_3d_format: Video3DFormat | None = Field(default=None, alias="Video3DFormat")
    media_streams: list[MediaStream] = Field(default_factory=list, alias="MediaStreams")
    bitrate: int | None = Field(default=None, alias="Bitrate")
    default_audio_stream_index: int | None = Field(default=None, alias="DefaultAudioStreamIndex")
    default_subtitle_stream_index: int | None = Field(default=None, alias="DefaultSubtitleStreamIndex")


# ============================================================================
# 播放信息模型 (Playback Info Models)
# ============================================================================


class NotificationPlaybackInfo(EmbyBaseModel):
    """通知播放信息"""

    played_to_completion: bool | None = Field(default=None, alias="PlayedToCompletion")
    position_ticks: int | None = Field(default=None, alias="PositionTicks")
    playlist_index: int | None = Field(default=None, alias="PlaylistIndex")
    playlist_length: int | None = Field(default=None, alias="PlaylistLength")
    play_session_id: str | None = Field(default=None, alias="PlaySessionId")
    media_source: MediaSourceInfo | None = Field(default=None, alias="MediaSource")


class TranscodingInfo(EmbyBaseModel):
    """转码信息"""

    audio_codec: str | None = Field(default=None, alias="AudioCodec")
    video_codec: str | None = Field(default=None, alias="VideoCodec")
    container: str | None = Field(default=None, alias="Container")
    is_video_direct: bool = Field(default=False, alias="IsVideoDirect")
    is_audio_direct: bool = Field(default=False, alias="IsAudioDirect")
    bitrate: int | None = Field(default=None, alias="Bitrate")
    framerate: float | None = Field(default=None, alias="Framerate")
    completion_percentage: float | None = Field(default=None, alias="CompletionPercentage")
    width: int | None = Field(default=None, alias="Width")
    height: int | None = Field(default=None, alias="Height")
    audio_channels: int | None = Field(default=None, alias="AudioChannels")
    transcode_reasons: list[TranscodeReason] = Field(default_factory=list, alias="TranscodeReasons")


# ============================================================================
# 设备信息模型 (Device Info Models)
# ============================================================================


class DeviceInfo(EmbyBaseModel):
    """设备信息"""

    name: str = Field(alias="Name")
    id: str | None = Field(default=None, alias="Id")  # 在 authenticationfailed 事件中不存在
    app_name: str | None = Field(default=None, alias="AppName")
    app_version: str | None = Field(default=None, alias="AppVersion")
    last_user_name: str | None = Field(default=None, alias="LastUserName")
    last_user_id: str | None = Field(default=None, alias="LastUserId")
    date_last_activity: datetime | None = Field(default=None, alias="DateLastActivity")
    icon_url: str | None = Field(default=None, alias="IconUrl")
    ip_address: str | None = Field(default=None, alias="IpAddress")


# ============================================================================
# LiveTV 相关模型 (LiveTV Models)
# ============================================================================


class BaseTimerInfoDto(EmbyBaseModel):
    """基础定时器信息"""

    id: str | None = Field(default=None, alias="Id")
    channel_id: str | None = Field(default=None, alias="ChannelId")
    channel_name: str | None = Field(default=None, alias="ChannelName")
    program_id: str | None = Field(default=None, alias="ProgramId")
    name: str | None = Field(default=None, alias="Name")
    overview: str | None = Field(default=None, alias="Overview")
    start_date: datetime | None = Field(default=None, alias="StartDate")
    end_date: datetime | None = Field(default=None, alias="EndDate")
    priority: int = Field(default=0, alias="Priority")
    pre_padding_seconds: int = Field(default=0, alias="PrePaddingSeconds")
    post_padding_seconds: int = Field(default=0, alias="PostPaddingSeconds")
    keep_until: KeepUntil | None = Field(default=None, alias="KeepUntil")


class TimerInfoDto(BaseTimerInfoDto):
    """定时器信息"""

    status: RecordingStatus | None = Field(default=None, alias="Status")
    series_timer_id: str | None = Field(default=None, alias="SeriesTimerId")
    run_time_ticks: int | None = Field(default=None, alias="RunTimeTicks")
    timer_type: TimerType | None = Field(default=None, alias="TimerType")


class SeriesTimerInfoDto(BaseTimerInfoDto):
    """系列定时器信息"""

    record_any_time: bool = Field(default=False, alias="RecordAnyTime")
    record_any_channel: bool = Field(default=False, alias="RecordAnyChannel")
    keep_up_to: int = Field(default=0, alias="KeepUpTo")
    record_new_only: bool = Field(default=False, alias="RecordNewOnly")
    days: list[DayOfWeek] = Field(default_factory=list, alias="Days")
    timer_type: TimerType | None = Field(default=None, alias="TimerType")


# ============================================================================
# 插件和更新模型 (Plugin & Update Models)
# ============================================================================


class PluginInfo(EmbyBaseModel):
    """插件信息"""

    name: str = Field(alias="Name")
    version: str = Field(alias="Version")
    id: str = Field(alias="Id")
    description: str | None = Field(default=None, alias="Description")
    image_tag: str | None = Field(default=None, alias="ImageTag")


class PackageVersionInfo(EmbyBaseModel):
    """包版本信息"""

    name: str | None = Field(default=None, alias="name")
    guid: str | None = Field(default=None, alias="guid")
    version_str: str | None = Field(default=None, alias="versionStr")
    classification: PackageVersionClass | None = Field(default=None, alias="classification")
    description: str | None = Field(default=None, alias="description")
    source_url: str | None = Field(default=None, alias="sourceUrl")
    timestamp: datetime | None = Field(default=None, alias="timestamp")


# ============================================================================
# 媒体项目模型 (Base Item Dto)
# ============================================================================


class ExternalUrl(EmbyBaseModel):
    """外部 URL"""

    name: str | None = Field(default=None, alias="Name")
    url: str | None = Field(default=None, alias="Url")


class MediaUrl(EmbyBaseModel):
    """媒体 URL"""

    name: str | None = Field(default=None, alias="Name")
    url: str | None = Field(default=None, alias="Url")


class NameIdPair(EmbyBaseModel):
    """名称ID对"""

    name: str | None = Field(default=None, alias="Name")
    id: str | None = Field(default=None, alias="Id")


class BaseItemPerson(EmbyBaseModel):
    """人员信息"""

    name: str | None = Field(default=None, alias="Name")
    id: str | None = Field(default=None, alias="Id")
    role: str | None = Field(default=None, alias="Role")
    type: str | None = Field(default=None, alias="Type")
    primary_image_tag: str | None = Field(default=None, alias="PrimaryImageTag")


class UserItemDataDto(EmbyBaseModel):
    """用户项目数据"""

    playback_position_ticks: int = Field(default=0, alias="PlaybackPositionTicks")
    play_count: int = Field(default=0, alias="PlayCount")
    is_favorite: bool = Field(default=False, alias="IsFavorite")
    played: bool = Field(default=False, alias="Played")
    last_played_date: datetime | None = Field(default=None, alias="LastPlayedDate")


class BaseItemDto(EmbyBaseModel):
    """
    基础媒体项目信息
    (MediaBrowser.Model.Dto.BaseItemDto)
    """

    # 基本信息
    name: str = Field(alias="Name")
    original_title: str | None = Field(default=None, alias="OriginalTitle")
    server_id: str = Field(alias="ServerId")
    id: str = Field(alias="Id")
    etag: str | None = Field(default=None, alias="Etag")
    date_created: datetime | None = Field(default=None, alias="DateCreated")
    date_modified: datetime | None = Field(default=None, alias="DateModified")

    # 媒体信息
    container: str | None = Field(default=None, alias="Container")
    sort_name: str | None = Field(default=None, alias="SortName")
    premiere_date: datetime | None = Field(default=None, alias="PremiereDate")
    path: str = Field(alias="Path")
    official_rating: str | None = Field(default=None, alias="OfficialRating")
    overview: str | None = Field(default=None, alias="Overview")
    taglines: list[str] = Field(default_factory=list, alias="Taglines")
    genres: list[str] = Field(default_factory=list, alias="Genres")
    community_rating: float | None = Field(default=None, alias="CommunityRating")
    critic_rating: float | None = Field(default=None, alias="CriticRating")
    run_time_ticks: int | None = Field(default=None, alias="RunTimeTicks")
    size: int | None = Field(default=None, alias="Size")
    file_name: str | None = Field(default=None, alias="FileName")
    bitrate: int | None = Field(default=None, alias="Bitrate")
    production_year: int | None = Field(default=None, alias="ProductionYear")

    # 索引
    index_number: int | None = Field(default=None, alias="IndexNumber")
    index_number_end: int | None = Field(default=None, alias="IndexNumberEnd")
    parent_index_number: int | None = Field(default=None, alias="ParentIndexNumber")

    # 类型和层级
    type: str = Field(alias="Type")
    is_folder: bool | None = Field(default=None, alias="IsFolder")
    parent_id: str | None = Field(default=None, alias="ParentId")
    media_type: str | None = Field(default=None, alias="MediaType")

    # 剧集相关
    series_name: str | None = Field(default=None, alias="SeriesName")
    series_id: str | None = Field(default=None, alias="SeriesId")
    season_id: str | None = Field(default=None, alias="SeasonId")
    season_name: str | None = Field(default=None, alias="SeasonName")

    # Provider IDs
    provider_ids: Annotated[dict[str, str], Field(default_factory=dict, alias="ProviderIds")]

    # 图像
    image_tags: Annotated[dict[str, str], Field(default_factory=dict, alias="ImageTags")]
    backdrop_image_tags: Annotated[list[str], Field(default_factory=list, alias="BackdropImageTags")]
    primary_image_aspect_ratio: float | None = Field(default=None, alias="PrimaryImageAspectRatio")

    # 分辨率
    width: int | None = Field(default=None, alias="Width")
    height: int | None = Field(default=None, alias="Height")

    # 关联数据
    external_urls: list[ExternalUrl] = Field(default_factory=list, alias="ExternalUrls")
    media_sources: list[MediaSourceInfo] = Field(default_factory=list, alias="MediaSources")
    media_streams: list[MediaStream] = Field(default_factory=list, alias="MediaStreams")
    remote_trailers: list[MediaUrl] = Field(default_factory=list, alias="RemoteTrailers")
    people: list[BaseItemPerson] = Field(default_factory=list, alias="People")
    tags: list[str] = Field(default_factory=list, alias="Tags")
    user_data: UserItemDataDto | None = Field(default=None, alias="UserData")

    # LiveTV 相关
    channel_id: str | None = Field(default=None, alias="ChannelId")
    channel_name: str | None = Field(default=None, alias="ChannelName")
    is_movie: bool | None = Field(default=None, alias="IsMovie")
    is_series: bool | None = Field(default=None, alias="IsSeries")
    is_live: bool | None = Field(default=None, alias="IsLive")
    start_date: datetime | None = Field(default=None, alias="StartDate")
    end_date: datetime | None = Field(default=None, alias="EndDate")

    # ========== 计算属性 ==========

    @property
    def runtime_seconds(self) -> float | None:
        """获取运行时长（秒）"""
        if self.run_time_ticks:
            return self.run_time_ticks / 10_000_000
        return None

    @property
    def runtime_str(self) -> str:
        """获取人类可读的运行时长，如 '1h 52m'"""
        if not self.run_time_ticks:
            return "N/A"
        total_seconds = self.run_time_ticks / 10_000_000
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @property
    def size_str(self) -> str:
        """获取人类可读的文件大小，如 '19.46 GB'"""
        if not self.size:
            return "N/A"
        size = float(self.size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PB"

    @property
    def resolution(self) -> str | None:
        """获取分辨率，如 '4K', '1080p'"""
        if self.width and self.height:
            if self.width >= 3840:
                return "4K"
            elif self.width >= 1920:
                return "1080p"
            elif self.width >= 1280:
                return "720p"
            elif self.width >= 720:
                return "480p"
            return f"{self.width}x{self.height}"
        return None

    @property
    def imdb_id(self) -> str | None:
        """获取 IMDB ID"""
        return self.provider_ids.get("Imdb")

    @property
    def tmdb_id(self) -> str | None:
        """获取 TMDB ID"""
        return self.provider_ids.get("Tmdb")

    @property
    def tvdb_id(self) -> str | None:
        """获取 TVDB ID"""
        return self.provider_ids.get("Tvdb")

    @property
    def imdb_url(self) -> str | None:
        """获取 IMDB URL"""
        if imdb_id := self.imdb_id:
            return f"https://www.imdb.com/title/{imdb_id}"
        return None

    @property
    def tmdb_url(self) -> str | None:
        """获取 TMDB URL"""
        if tmdb_id := self.tmdb_id:
            if self.type == "Movie":
                return f"https://www.themoviedb.org/movie/{tmdb_id}"
            elif self.type in ("Series", "Season", "Episode"):
                return f"https://www.themoviedb.org/tv/{tmdb_id}"
        return None

    @property
    def display_name(self) -> str:
        """获取显示名称，对于剧集包含剧名和集号"""
        if self.type == "Episode":
            parts = []
            if self.series_name:
                parts.append(self.series_name)
            if self.parent_index_number is not None:
                parts.append(f"S{self.parent_index_number:02d}")
            if self.index_number is not None:
                parts.append(f"E{self.index_number:02d}")
            if self.name:
                parts.append(self.name)
            return " - ".join(parts) if parts else self.name or "Unknown"
        return self.name or "Unknown"

    @property
    def primary_image_tag_value(self) -> str | None:
        """获取主图像标签"""
        return self.image_tags.get("Primary")


# 处理循环引用
BaseItemDto.model_rebuild()


# ============================================================================
# 基础 Webhook 事件模型
# ============================================================================


class BaseWebhookEvent(EmbyBaseModel):
    """所有事件的基础模型"""

    title: str = Field(alias="Title")
    description: str | None = Field(default=None, alias="Description")
    url: str | None = Field(default=None, alias="Url")
    date: datetime = Field(alias="Date")
    severity: LogSeverity = Field(alias="Severity")
    server: NotificationServerInfo = Field(alias="Server")


# ============================================================================
# 播放事件 (Playback Events)
# ============================================================================


class PlaybackStartEvent(BaseWebhookEvent):
    """playback.start 事件 - 用户开始播放"""

    event: Literal["playback.start"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")
    user: NotificationUserDto = Field(alias="User")
    session: NotificationSessionInfo = Field(alias="Session")
    playback_info: NotificationPlaybackInfo | None = Field(default=None, alias="PlaybackInfo")
    device_info: DeviceInfo | None = Field(default=None, alias="DeviceInfo")


class PlaybackStopEvent(BaseWebhookEvent):
    """playback.stop 事件 - 用户停止播放"""

    event: Literal["playback.stop"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")
    user: NotificationUserDto = Field(alias="User")
    session: NotificationSessionInfo = Field(alias="Session")
    playback_info: NotificationPlaybackInfo = Field(alias="PlaybackInfo")
    device_info: DeviceInfo | None = Field(default=None, alias="DeviceInfo")


class PlaybackProgressEvent(BaseWebhookEvent):
    """playback.progress 事件 - 播放进度更新"""

    event: Literal["playback.progress"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")
    user: NotificationUserDto = Field(alias="User")
    session: NotificationSessionInfo = Field(alias="Session")
    playback_info: NotificationPlaybackInfo = Field(alias="PlaybackInfo")
    transcoding_info: TranscodingInfo | None = Field(default=None, alias="TranscodingInfo")


class PlaybackPauseEvent(BaseWebhookEvent):
    """playback.pause 事件 - 用户暂停播放"""

    event: Literal["playback.pause"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")
    user: NotificationUserDto = Field(alias="User")
    session: NotificationSessionInfo = Field(alias="Session")


class PlaybackUnpauseEvent(BaseWebhookEvent):
    """playback.unpause 事件 - 用户恢复播放"""

    event: Literal["playback.unpause"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")
    user: NotificationUserDto = Field(alias="User")
    session: NotificationSessionInfo = Field(alias="Session")


# ============================================================================
# 媒体库事件 (Library Events)
# ============================================================================


class LibraryNewEvent(BaseWebhookEvent):
    """library.new 事件 - 新增媒体"""

    event: Literal["library.new"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")


class LibraryDeletedEvent(BaseWebhookEvent):
    """library.deleted 事件 - 删除媒体"""

    event: Literal["library.deleted"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")


class LibraryUpdatedEvent(BaseWebhookEvent):
    """library.updated 事件 - 更新媒体"""

    event: Literal["library.updated"] = Field(alias="Event")
    item: BaseItemDto = Field(alias="Item")


# ============================================================================
# 用户事件 (User Events)
# ============================================================================


class UserAuthenticatedEvent(BaseWebhookEvent):
    """user.authenticated 事件 - 用户登录"""

    event: Literal["user.authenticated"] = Field(alias="Event")
    user: NotificationUserDto = Field(alias="User")
    session: NotificationSessionInfo | None = Field(default=None, alias="Session")
    device_info: DeviceInfo | None = Field(default=None, alias="DeviceInfo")


class UserLockedOutEvent(BaseWebhookEvent):
    """user.lockedout 事件 - 用户被锁定"""

    event: Literal["user.lockedout"] = Field(alias="Event")
    user: NotificationUserDto = Field(alias="User")


class UserCreatedEvent(BaseWebhookEvent):
    """user.created 事件 - 创建用户"""

    event: Literal["user.created"] = Field(alias="Event")
    user: NotificationUserDto = Field(alias="User")


class UserDeletedEvent(BaseWebhookEvent):
    """user.deleted 事件 - 删除用户"""

    event: Literal["user.deleted"] = Field(alias="Event")
    user: NotificationUserDto = Field(alias="User")


class UserPasswordChangedEvent(BaseWebhookEvent):
    """user.passwordchanged 事件 - 用户密码修改"""

    event: Literal["user.passwordchanged"] = Field(alias="Event")
    user: NotificationUserDto = Field(alias="User")


class UserAuthenticationFailedEvent(BaseWebhookEvent):
    """user.authenticationfailed 事件 - 用户登录失败"""

    event: Literal["user.authenticationfailed"] = Field(alias="Event")
    device_info: DeviceInfo | None = Field(default=None, alias="DeviceInfo")


class UserPolicyUpdatedEvent(BaseWebhookEvent):
    """user.policyupdated 事件 - 用户策略更新"""

    event: Literal["user.policyupdated"] = Field(alias="Event")
    user: NotificationUserDto = Field(alias="User")


# ============================================================================
# 系统事件 (System Events)
# ============================================================================


class SystemUpdateAvailableEvent(BaseWebhookEvent):
    """system.updateavailable 事件 - 有可用更新"""

    event: Literal["system.updateavailable"] = Field(alias="Event")
    package_version_info: PackageVersionInfo | None = Field(default=None, alias="PackageVersionInfo")


class SystemRestartRequiredEvent(BaseWebhookEvent):
    """system.restartrequired 事件 - 需要重启 (旧版)"""

    event: Literal["system.restartrequired"] = Field(alias="Event")


class SystemServerRestartRequiredEvent(BaseWebhookEvent):
    """system.serverrestartrequired 事件 - 需要重启服务器"""

    event: Literal["system.serverrestartrequired"] = Field(alias="Event")


class SystemStartedEvent(BaseWebhookEvent):
    """system.started 事件 - 系统启动 (旧版)"""

    event: Literal["system.started"] = Field(alias="Event")


class SystemServerStartupEvent(BaseWebhookEvent):
    """system.serverstartup 事件 - 服务器启动"""

    event: Literal["system.serverstartup"] = Field(alias="Event")


class SystemShuttingDownEvent(BaseWebhookEvent):
    """system.shuttingdown 事件 - 系统关闭中"""

    event: Literal["system.shuttingdown"] = Field(alias="Event")


class SystemWebhookTestEvent(BaseWebhookEvent):
    """system.webhooktest 事件 - Webhook 测试 (旧版)"""

    event: Literal["system.webhooktest"] = Field(alias="Event")


class SystemNotificationTestEvent(BaseWebhookEvent):
    """system.notificationtest 事件 - 通知测试"""

    event: Literal["system.notificationtest"] = Field(alias="Event")


# ============================================================================
# 录制事件 (Recording Events - LiveTV)
# ============================================================================


class RecordingStartedEvent(BaseWebhookEvent):
    """recording.started 事件 - 开始录制"""

    event: Literal["recording.started"] = Field(alias="Event")
    item: BaseItemDto | None = Field(default=None, alias="Item")
    recording_info: NotificationRecordingInfo | None = Field(default=None, alias="RecordingInfo")
    timer_info: TimerInfoDto | None = Field(default=None, alias="TimerInfo")


class RecordingCompletedEvent(BaseWebhookEvent):
    """recording.completed 事件 - 录制完成"""

    event: Literal["recording.completed"] = Field(alias="Event")
    item: BaseItemDto | None = Field(default=None, alias="Item")
    recording_info: NotificationRecordingInfo | None = Field(default=None, alias="RecordingInfo")


class RecordingCancelledEvent(BaseWebhookEvent):
    """recording.cancelled 事件 - 录制取消"""

    event: Literal["recording.cancelled"] = Field(alias="Event")
    timer_info: TimerInfoDto | None = Field(default=None, alias="TimerInfo")


class RecordingFailedEvent(BaseWebhookEvent):
    """recording.failed 事件 - 录制失败"""

    event: Literal["recording.failed"] = Field(alias="Event")
    recording_info: NotificationRecordingInfo | None = Field(default=None, alias="RecordingInfo")


# ============================================================================
# 插件事件 (Plugin Events)
# ============================================================================


class PluginInstalledEvent(BaseWebhookEvent):
    """plugin.installed 事件 - 插件安装 (旧版)"""

    event: Literal["plugin.installed"] = Field(alias="Event")
    plugin_info: PluginInfo = Field(alias="PluginInfo")


class PluginsPluginInstalledEvent(BaseWebhookEvent):
    """plugins.plugininstalled 事件 - 插件安装"""

    event: Literal["plugins.plugininstalled"] = Field(alias="Event")
    package_version_info: PackageVersionInfo | None = Field(default=None, alias="PackageVersionInfo")


class PluginUpdatedEvent(BaseWebhookEvent):
    """plugin.updated 事件 - 插件更新"""

    event: Literal["plugin.updated"] = Field(alias="Event")
    plugin_info: PluginInfo = Field(alias="PluginInfo")


class PluginUninstalledEvent(BaseWebhookEvent):
    """plugin.uninstalled 事件 - 插件卸载"""

    event: Literal["plugin.uninstalled"] = Field(alias="Event")
    plugin_info: PluginInfo = Field(alias="PluginInfo")


# ============================================================================
# 任务事件 (Task Events)
# ============================================================================


class TaskCompletedEvent(BaseWebhookEvent):
    """task.completed 事件 - 任务完成 (旧版)"""

    event: Literal["task.completed"] = Field(alias="Event")


class ScheduledTasksCompletedEvent(BaseWebhookEvent):
    """scheduledtasks.completed 事件 - 计划任务完成"""

    event: Literal["scheduledtasks.completed"] = Field(alias="Event")


class TaskFailedEvent(BaseWebhookEvent):
    """task.failed 事件 - 任务失败"""

    event: Literal["task.failed"] = Field(alias="Event")


# ============================================================================
# 通用事件 (用于未知事件类型)
# ============================================================================


class GenericWebhookEvent(BaseWebhookEvent):
    """通用事件 - 用于未定义的事件类型"""

    event: str = Field(alias="Event")
    item: BaseItemDto | None = Field(default=None, alias="Item")
    user: NotificationUserDto | None = Field(default=None, alias="User")
    session: NotificationSessionInfo | None = Field(default=None, alias="Session")
    playback_info: NotificationPlaybackInfo | None = Field(default=None, alias="PlaybackInfo")
    transcoding_info: TranscodingInfo | None = Field(default=None, alias="TranscodingInfo")
    device_info: DeviceInfo | None = Field(default=None, alias="DeviceInfo")
    recording_info: NotificationRecordingInfo | None = Field(default=None, alias="RecordingInfo")
    timer_info: TimerInfoDto | None = Field(default=None, alias="TimerInfo")
    series_timer_info: SeriesTimerInfoDto | None = Field(default=None, alias="SeriesTimerInfo")
    plugin_info: PluginInfo | None = Field(default=None, alias="PluginInfo")
    package_version_info: PackageVersionInfo | None = Field(default=None, alias="PackageVersionInfo")
    program_info: BaseItemDto | None = Field(default=None, alias="ProgramInfo")


# ============================================================================
# Discriminated Union - 自动选择正确的模型
# ============================================================================


# 所有已知事件的联合类型
EmbyPayload = Annotated[
    # 播放事件
    PlaybackStartEvent
    | PlaybackStopEvent
    | PlaybackProgressEvent
    | PlaybackPauseEvent
    | PlaybackUnpauseEvent
    # 媒体库事件
    | LibraryNewEvent
    | LibraryDeletedEvent
    | LibraryUpdatedEvent
    # 用户事件
    | UserAuthenticatedEvent
    | UserLockedOutEvent
    | UserCreatedEvent
    | UserDeletedEvent
    | UserPasswordChangedEvent
    | UserAuthenticationFailedEvent
    | UserPolicyUpdatedEvent
    # 系统事件
    | SystemUpdateAvailableEvent
    | SystemRestartRequiredEvent
    | SystemServerRestartRequiredEvent
    | SystemStartedEvent
    | SystemServerStartupEvent
    | SystemShuttingDownEvent
    | SystemWebhookTestEvent
    | SystemNotificationTestEvent
    # 录制事件
    | RecordingStartedEvent
    | RecordingCompletedEvent
    | RecordingCancelledEvent
    | RecordingFailedEvent
    # 插件事件
    | PluginInstalledEvent
    | PluginsPluginInstalledEvent
    | PluginUpdatedEvent
    | PluginUninstalledEvent
    # 任务事件
    | TaskCompletedEvent
    | ScheduledTasksCompletedEvent
    | TaskFailedEvent,
    Field(discriminator="event"),
]


# ============================================================================
# 导出
# ============================================================================

__all__ = [
    # 枚举
    "LogSeverity",
    "RecordingStatus",
    "ImageType",
    "TimerType",
    "KeepUntil",
    "PackageVersionClass",
    "MediaStreamType",
    "MediaProtocol",
    "Video3DFormat",
    "LocationType",
    "MediaSourceType",
    "TranscodeReason",
    "DayOfWeek",
    # 基础模型
    "EmbyBaseModel",
    "NotificationUserDto",
    "NotificationServerInfo",
    "NotificationSessionInfo",
    "NotificationRecordingInfo",
    "NotificationPlaybackInfo",
    "MediaStream",
    "ChapterInfo",
    "MediaSourceInfo",
    "TranscodingInfo",
    "DeviceInfo",
    "BaseTimerInfoDto",
    "TimerInfoDto",
    "SeriesTimerInfoDto",
    "PluginInfo",
    "PackageVersionInfo",
    "ExternalUrl",
    "MediaUrl",
    "NameIdPair",
    "BaseItemPerson",
    "UserItemDataDto",
    "BaseItemDto",
    # 事件基类
    "BaseWebhookEvent",
    # 播放事件
    "PlaybackStartEvent",
    "PlaybackStopEvent",
    "PlaybackProgressEvent",
    "PlaybackPauseEvent",
    "PlaybackUnpauseEvent",
    # 媒体库事件
    "LibraryNewEvent",
    "LibraryDeletedEvent",
    "LibraryUpdatedEvent",
    # 用户事件
    "UserAuthenticatedEvent",
    "UserLockedOutEvent",
    "UserCreatedEvent",
    "UserDeletedEvent",
    "UserPasswordChangedEvent",
    "UserAuthenticationFailedEvent",
    "UserPolicyUpdatedEvent",
    # 系统事件
    "SystemUpdateAvailableEvent",
    "SystemRestartRequiredEvent",
    "SystemServerRestartRequiredEvent",
    "SystemStartedEvent",
    "SystemServerStartupEvent",
    "SystemShuttingDownEvent",
    "SystemWebhookTestEvent",
    "SystemNotificationTestEvent",
    # 录制事件
    "RecordingStartedEvent",
    "RecordingCompletedEvent",
    "RecordingCancelledEvent",
    "RecordingFailedEvent",
    # 插件事件
    "PluginInstalledEvent",
    "PluginsPluginInstalledEvent",
    "PluginUpdatedEvent",
    "PluginUninstalledEvent",
    # 任务事件
    "TaskCompletedEvent",
    "ScheduledTasksCompletedEvent",
    "TaskFailedEvent",
    # 通用
    "GenericWebhookEvent",
    # 类型
    "EmbyPayload",
]
