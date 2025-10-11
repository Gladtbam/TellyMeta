from __future__ import annotations

from pydantic import BaseModel, Field


class EmbyItem(BaseModel):
    """Emby Webhook Item 模型"""
    Name: str
    Id: str
    Path: str | None = None

class EmbyPayload(BaseModel):
    """Emby Webhook 接收数据模型"""
    Event: str
    Item: EmbyItem | None = None

class UserPolicy(BaseModel):
    """Emby 用户策略模型"""
    IsAdministrator: bool = False
    IsHidden: bool = True
    IsHiddenRemotely: bool = True
    IsHiddenFromUnusedDevices: bool = True
    IsDisabled: bool = False
    LockedOutDate: int = 0
    MaxParentalRating: int | None = None
    AllowTagOrRating: bool = False
    BlockedTags: list[str] = Field(default_factory=list)
    IsTagBlockingModeInclusive: bool = False
    IncludeTags: list[str] = Field(default_factory=list)
    EnableUserPreferenceAccess: bool = True
    AccessSchedules: list[AccessSchedule] = Field(default_factory=list)
    BlockUnratedItems: list[str] = Field(default_factory=list)
    EnableRemoteControlOfOtherUsers: bool = False
    EnableSharedDeviceControl: bool = False
    EnableRemoteAccess: bool = True
    EnableLiveTvManagement: bool = False
    EnableLiveTvAccess: bool = True
    EnableMediaPlayback: bool = True
    EnableAudioPlaybackTranscoding: bool = False
    EnableVideoPlaybackTranscoding: bool = False
    EnablePlaybackRemuxing: bool = False
    EnableContentDeletion: bool = False
    RestrictedFeatures: list[str] = Field(default_factory=list)
    EnableContentDeletionFromFolders: list[str] = Field(default_factory=list)
    EnableContentDownloading: bool = False
    EnableSubtitleDownloading: bool = False
    EnableSubtitleManagement: bool = False
    EnableSyncTranscoding: bool = False
    EnableMediaConversion: bool = False
    EnabledChannels: list[str] = Field(default_factory=list)
    EnableAllChannels: bool = True
    EnabledFolders: list[str] = Field(default_factory=list)
    EnableAllFolders: bool = True
    InvalidLoginAttemptCount: int = 0
    EnablePublicSharing: bool = False
    RemoteClientBitrateLimit: int = 0
    AuthenticationProviderId: str | None = None # Emby.Server.Implementations.Library.DefaultAuthenticationProvider
    ExcludedSubFolders: list[str] = Field(default_factory=list)
    SimultaneousStreamLimit: int = 0
    EnabledDevices: list[str] = Field(default_factory=list)
    EnableAllDevices: bool = True
    AllowCameraUpload: bool = False
    AllowSharingPersonalItems: bool = False

class AccessSchedule(BaseModel):
    """Emby 访问时间模型"""
    DayOfWeek: str
    StartHour: float
    EndHour: float

class UserConfiguration(BaseModel):
    """Emby 用户配置模型"""
    AudioLanguagePreference: str
    PlayDefaultAudioTrack: bool
    SubtitleLanguagePreference: str
    ProfilePin: str
    DisplayMissingEpisodes: bool
    SubtitleMode: str
    OrderedViews: list[str] = Field(default_factory=list)
    LatestItemsExcludes: list[str] = Field(default_factory=list)
    MyMediaExcludes: list[str] = Field(default_factory=list)
    HidePlayedInLatest: bool
    HidePlayedInMoreLikeThis: bool
    HidePlayedInSuggestions: bool
    RememberAudioSelections: bool
    RememberSubtitleSelections: bool
    EnableNextEpisodeAutoPlay: bool
    ResumeRewindSeconds: int
    IntroSkipMode: str

class UserDto(BaseModel):
    """Emby 用户模型"""
    Id: str
    Name: str
    ServerId: str
    ServerName: str
    Prefix: str
    ConnectUserName: str
    DateCreated: str | None
    ConnectLinkType: str | None
    PrimaryImageTag: str
    HasPassword: bool
    HasConfiguredPassword: bool
    EnableAutoLogin: bool | None
    LastLoginDate: str | None
    LastActivityDate: str | None
    Configuration: UserConfiguration
    Policy: UserPolicy
    PrimaryImageAspectRatio: float | None
    UserItemShareLevel: str | None

class QueryResult_BaseItemDto(BaseModel):
    """Emby 搜索结果模型"""
    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int

class BaseItemDto(BaseModel):
    """Emby 基础媒体项模型"""
    Name: str
    OriginalTitle: str
    ServerId: str
    Id: str
    Guid: str
    Etag: str
    Prefix: str
    TunerName: str
    PlaylistItemId: str
    DateCreated: str | None
    DateModified: str | None
    VideoCodec: str
    AudioCodec: str
    AverageFrameRate: float | None
    RealFrameRate: float | None
    ExtraType: str
    SortIndexNumber: int | None
    SortParentIndexNumber: int | None
    CanDelete: bool | None
    CanDownload: bool | None
    CanEditItems: bool | None
    SupportsResume: bool | None
    PresentationUniqueKey: str
    PreferredMetadataLanguage: str
    PreferredMetadataCountryCode: str
    SupportsSync: bool | None
    SyncStatus: str
    CanManageAccess: bool | None
    CanLeaveContent: bool | None
    CanMakePublic: bool | None
    Container: str
    SortName: str
    ForcedSortName: str
    Video3DFormat: str
    PremiereDate: str | None
    ExternalUrls: list[ExternalUrls] = Field(default_factory=list)
    MediaSources: list[MediaSourceInfo] = Field(default_factory=list)
    CriticRating: float | None
    GameSystemId: int | None
    AsSeries: bool | None
    GameSystem: str
    ProductionLocations: list[str] = Field(default_factory=list)
    Path: str
    OfficialRating: str
    CustomRating: str
    ChannelId: str
    ChannelName: str
    Overview: str
    Taglines: list[str] = Field(default_factory=list)
    Genres: list[str] = Field(default_factory=list)
    CommunityRating: float | None
    RunTimeTicks: int | None
    Size: int | None
    FileName: str
    Bitrate: int | None
    ProductionYear: int | None
    Number: str
    ChannelNumber: str
    IndexNumber: int | None
    IndexNumberEnd: int | None
    ParentIndexNumber: int | None
    RemoteTrailers: list[ExternalUrls] = Field(default_factory=list) # MediaUrl
    ProviderIds: dict[str, str] = Field(default_factory=dict)
    IsFolder: bool | None
    ParentId: str
    Type: str
    People: list[BaseItemPerson] = Field(default_factory=list)
    Studios: list[NameLongIdPair] = Field(default_factory=list)
    GenreItems: list[NameLongIdPair] = Field(default_factory=list)
    TagItems: list[NameLongIdPair] = Field(default_factory=list)
    ParentLogoItemId: str
    ParentBackdropItemId: str
    ParentBackdropImageTags: list[str] = Field(default_factory=list)
    LocalTrailerCount: int | None
    UserData: UserItemDataDto
    RecursiveItemCount: int | None
    ChildCount: int | None
    SeasonCount: int | None
    SeriesName: str
    SeriesId: str
    SeasonId: str
    SpecialFeatureCount: int | None
    DisplayPreferencesId: str
    Status: str
    AirDays: list[str] = Field(default_factory=list)
    Tags: list[str] = Field(default_factory=list)
    PrimaryImageAspectRatio: float | None
    Artists: list[str] = Field(default_factory=list)
    ArtistItems: list[NameLongIdPair] = Field(default_factory=list) # NameIdPair
    Composers: list[NameLongIdPair] = Field(default_factory=list) # NameIdPair
    Album: str
    CollectionType: str
    DisplayOrder: str
    AlbumId: str
    AlbumPrimaryImageTag: str
    SeriesPrimaryImageTag: str
    AlbumArtist: str
    AlbumArtists: list[NameLongIdPair] = Field(default_factory=list) # NameIdPair
    SeasonName: str
    MediaStreams: list[MediaStream] = Field(default_factory=list)
    PartCount: int | None
    ImageTags: dict[str, str] = Field(default_factory=dict)
    BackdropImageTags: list[str] = Field(default_factory=list)
    ParentLogoImageTag: str
    SeriesStudio: str
    PrimaryImageItemId: str
    PrimaryImageTag: str
    ParentThumbItemId: str
    ParentThumbImageTag: str
    Chapters: list[ChapterInfo] = Field(default_factory=list)
    LocationType: str
    MediaType: str
    EndDate: str | None
    LockedFields: list[str] = Field(default_factory=list)
    LockData: bool | None
    Width: int | None
    Height: int | None
    CameraMake: str
    CameraModel: str
    Software: str
    ExposureTime: float | None
    FocalLength: float | None
    ImageOrientation: str
    Aperture: float | None
    ShutterSpeed: float | None
    Latitude: float | None
    Longitude: float | None
    Altitude: float | None
    IsoSpeedRating: int | None
    SeriesTimerId: str
    ChannelPrimaryImageTag: str
    StartDate: str | None
    CompletionPercentage: float | None
    IsRepeat: bool | None
    IsNew: bool | None
    EpisodeTitle: str
    IsMovie: bool | None
    IsSports: bool | None
    IsSeries: bool | None
    IsLive: bool | None
    IsNews: bool | None
    IsKids: bool | None
    IsPremiere: bool | None
    TimerType: str
    Disabled: bool | None
    ManagementId: str
    TimerId: str
    CurrentProgram: BaseItemDto | str | None
    MovieCount: int | None
    SeriesCount: int | None
    AlbumCount: int | None
    SongCount: int | None
    MusicVideoCount: int | None
    Subviews: list[str] = Field(default_factory=list)
    ListingsProviderId: str
    ListingsChannelId: str
    ListingsPath: str
    ListingsId: str
    ListingsChannelName: str
    ListingsChannelNumber: str
    AffiliateCallSign: str

class ExternalUrls(BaseModel):
    """Emby 外部链接模型
    MediaUrl 与其相同，直接使用
    """
    Name: str
    Url: str

class MediaSourceInfo(BaseModel):
    """Emby 媒体源模型"""
    Chapters: list[ChapterInfo] = Field(default_factory=list)
    Protocol: str
    Id: str
    Path: str
    EncoderPath: str
    EncoderProtocol: str
    Type: str
    ProbePath: str
    ProbeProtocol: str
    Container: str
    Size: int | None
    Name: str
    SortName: str
    IsRemote: bool
    HasMixedProtocols: bool
    RunTimeTicks: int | None
    ContainerStartTimeTicks: int | None
    SupportsTranscoding: bool
    TrancodeLiveStartIndex: int | None
    WallClockStart: str | None
    SupportsDirectStream: bool
    SupportsDirectPlay: bool
    IsInfiniteStream: bool
    RequiresOpening: bool
    OpenToken: str
    RequiresClosing: bool
    LiveStreamId: str
    RequiresLooping: bool
    Video3DFormat: str
    MediaStreams: list[MediaStream] = Field(default_factory=list)
    Formats: list[str] = Field(default_factory=list)
    Bitrate: int | None
    Timestamp: str
    RequiredHttpHeaders: dict[str, str] = Field(default_factory=dict)
    DirectStreamUrl: str
    AddApiKeyToDirectStreamUrl: bool
    TranscodingUrl: str
    TranscodingSubProtocol: str
    TranscodingContainer: str
    DefaultAudioStreamIndex: int | None
    DefaultSubtitleStreamIndex: int | None
    ItemId: str
    ServerId: str

class ChapterInfo(BaseModel):
    """Emby 章节信息模型"""
    StartPositionTicks: int
    Name: str
    ImageTag: str
    MarkerType: str
    ChapterIndex: int

class MediaStream(BaseModel):
    """Emby 媒体流模型"""
    Codec: str
    CodecTag: str
    Language: str
    ColorTransfer: str
    ColorPrimaries: str
    ColorSpace: str
    Comment: str
    StreamStartTimeTicks: int | None
    TimeBase: str
    Title: str
    Extradata: str
    VideoRange: str
    DisplayTitle: str
    DisplayLanguage: str
    NalLengthSize: str
    IsInterlaced: bool
    ChannelLayout: str
    BitRate: int | None
    BitDepth: int | None
    RefFrames: int | None
    Rotation: int | None
    Channels: int | None
    SampleRate: int | None
    IsDefault: bool
    IsForced: bool
    IsHearingImpaired: bool
    Height: int | None
    Width: int | None
    AverageFrameRate: float | None
    RealFrameRate: float | None
    Profile: str
    Type: str
    AspectRatio: str
    Index: int
    IsExternal: bool
    DeliveryMethod: str
    DeliveryUrl: str
    IsExternalUrl: bool | None
    IsTextSubtitleStream: bool
    SupportsExternalStream: bool
    Path: str
    Protocol: str
    PixelFormat: str
    Level: int | None
    IsAnamorphic: bool | None
    ExtendedVideoType: str
    ExtendedVideoSubType: str
    ExtendedVideoSubTypeDescription: str
    ItemId: str
    ServerId: str
    AttachmentSize: int | None
    MimeType: str
    SubtitleLocationType: str

class BaseItemPerson(BaseModel):
    """Emby 媒体项人员模型"""
    Name: str
    Id: str
    Role: str
    Type: str
    PrimaryImageTag: str

class NameLongIdPair(BaseModel):
    """Emby 名称与长ID对模型
    NameIdPair 的 Id 为 str
    """
    Name: str
    Id: int | str

class UserItemDataDto(BaseModel):
    """Emby 用户媒体项数据模型"""
    Rating: float | None
    PlayedPercentage: float | None
    UnplayedItemCount: int | None
    PlaybackPositionTicks: int
    PlayCount: int | None
    IsFavorite: bool
    LastPlayedDate: str | None
    Played: bool
    Key: str
    ItemId: str
    ServerId: str
