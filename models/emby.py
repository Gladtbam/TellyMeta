# from __future__ import annotations

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

class AccessSchedule(BaseModel):
    """Emby 访问时间模型"""
    DayOfWeek: str
    StartHour: float
    EndHour: float

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

class UserConfiguration(BaseModel):
    """Emby 用户配置模型"""
    AudioLanguagePreference: str | None = None
    PlayDefaultAudioTrack: bool
    SubtitleLanguagePreference: str | None = None
    ProfilePin: str | None = None
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
    ServerName: str | None = None
    Prefix: str
    ConnectUserName: str | None = None
    DateCreated: str | None = None
    ConnectLinkType: str | None = None
    PrimaryImageTag: str | None = None
    HasPassword: bool
    HasConfiguredPassword: bool
    EnableAutoLogin: bool | None = None
    LastLoginDate: str | None = None
    LastActivityDate: str | None = None
    Configuration: UserConfiguration
    Policy: UserPolicy
    PrimaryImageAspectRatio: float | None = None
    UserItemShareLevel: str | None = None

class ExternalUrl(BaseModel):
    """Emby 外部链接模型
    MediaUrl 与其相同，直接使用
    """
    Name: str
    Url: str

class ChapterInfo(BaseModel):
    """Emby 章节信息模型"""
    StartPositionTicks: int
    Name: str
    ImageTag: str | None = None
    MarkerType: str
    ChapterIndex: int

class MediaStream(BaseModel):
    """Emby 媒体流模型"""
    Codec: str
    CodecTag: str | None = None
    Language: str | None = None
    ColorTransfer: str | None = None
    ColorPrimaries: str | None = None
    ColorSpace: str | None = None
    Comment: str | None = None
    StreamStartTimeTicks: int | None = None
    TimeBase: str
    Title: str | None = None
    Extradata: str | None = None
    VideoRange: str | None = None
    DisplayTitle: str
    DisplayLanguage: str | None = None
    NalLengthSize: str | None = None
    IsInterlaced: bool
    ChannelLayout: str | None = None
    BitRate: int | None = None
    BitDepth: int | None = None
    RefFrames: int | None = None
    Rotation: int | None = None
    Channels: int | None = None
    SampleRate: int | None = None
    IsDefault: bool
    IsForced: bool
    IsHearingImpaired: bool
    Height: int | None = None
    Width: int | None = None
    AverageFrameRate: float | None = None
    RealFrameRate: float | None = None
    Profile: str
    Type: str
    AspectRatio: str | None = None
    Index: int
    IsExternal: bool
    DeliveryMethod: str | None = None
    DeliveryUrl: str | None = None
    IsExternalUrl: bool | None = None
    IsTextSubtitleStream: bool
    SupportsExternalStream: bool
    Path: str | None = None
    Protocol: str
    PixelFormat: str | None = None
    Level: int | None = None
    IsAnamorphic: bool | None = None
    ExtendedVideoType: str
    ExtendedVideoSubType: str
    ExtendedVideoSubTypeDescription: str
    ItemId: str | None = None
    ServerId: str | None = None
    AttachmentSize: int | None = None
    MimeType: str | None = None
    SubtitleLocationType: str | None = None

class MediaSourceInfo(BaseModel):
    """Emby 媒体源模型"""
    Chapters: list[ChapterInfo] = Field(default_factory=list)
    Protocol: str
    Id: str
    Path: str
    EncoderPath: str | None = None
    EncoderProtocol: str | None = None
    Type: str
    ProbePath: str | None = None
    ProbeProtocol: str | None = None
    Container: str
    Size: int | None = None
    Name: str
    SortName: str | None = None
    IsRemote: bool
    HasMixedProtocols: bool
    RunTimeTicks: int | None = None
    ContainerStartTimeTicks: int | None = None
    SupportsTranscoding: bool
    TrancodeLiveStartIndex: int | None = None
    WallClockStart: str | None = None
    SupportsDirectStream: bool
    SupportsDirectPlay: bool
    IsInfiniteStream: bool
    RequiresOpening: bool
    OpenToken: str | None = None
    RequiresClosing: bool
    LiveStreamId: str | None = None
    RequiresLooping: bool
    Video3DFormat: str | None = None
    MediaStreams: list[MediaStream] = Field(default_factory=list)
    Formats: list[str] = Field(default_factory=list)
    Bitrate: int | None = None
    Timestamp: str | None = None
    RequiredHttpHeaders: dict[str, str] = Field(default_factory=dict)
    DirectStreamUrl: str | None = None
    AddApiKeyToDirectStreamUrl: bool
    TranscodingUrl: str | None = None
    TranscodingSubProtocol: str | None = None
    TranscodingContainer: str | None = None
    DefaultAudioStreamIndex: int | None = None
    DefaultSubtitleStreamIndex: int | None = None
    ItemId: str
    ServerId: str | None = None

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
    Rating: float | None = None
    PlayedPercentage: float | None = None
    UnplayedItemCount: int | None = None
    PlaybackPositionTicks: int
    PlayCount: int | None = None
    IsFavorite: bool
    LastPlayedDate: str | None = None
    Played: bool
    Key: str
    ItemId: str
    ServerId: str

class BaseItemDto(BaseModel):
    """Emby 基础媒体项模型"""
    Name: str
    OriginalTitle: str | None = None
    ServerId: str
    Id: str
    Guid: str | None = None
    Etag: str | None = None
    Prefix: str | None = None
    TunerName: str | None = None
    PlaylistItemId: str | None = None
    DateCreated: str | None = None
    DateModified: str | None = None
    VideoCodec: str | None = None
    AudioCodec: str | None = None
    AverageFrameRate: float | None = None
    RealFrameRate: float | None = None
    ExtraType: str | None = None
    SortIndexNumber: int | None = None
    SortParentIndexNumber: int | None = None
    CanDelete: bool | None = None
    CanDownload: bool | None = None
    CanEditItems: bool | None = None
    SupportsResume: bool | None = None
    PresentationUniqueKey: str | None = None
    PreferredMetadataLanguage: str | None = None
    PreferredMetadataCountryCode: str | None = None
    SupportsSync: bool | None = None
    SyncStatus: str | None = None
    CanManageAccess: bool | None = None
    CanLeaveContent: bool | None = None
    CanMakePublic: bool | None = None
    Container: str
    SortName: str
    ForcedSortName: str | None = None
    Video3DFormat: str | None = None
    PremiereDate: str | None = None
    ExternalUrls: list[ExternalUrl] = Field(default_factory=list)
    MediaSources: list[MediaSourceInfo] = Field(default_factory=list)
    CriticRating: float | None = None
    GameSystemId: int | None = None
    AsSeries: bool | None = None
    GameSystem: str | None = None
    ProductionLocations: list[str] = Field(default_factory=list)
    Path: str
    OfficialRating: str | None = None
    CustomRating: str | None = None
    ChannelId: str | None = None
    ChannelName: str | None = None
    Overview: str
    Taglines: list[str] = Field(default_factory=list)
    Genres: list[str] = Field(default_factory=list)
    CommunityRating: float | None = None
    RunTimeTicks: int | None = None
    Size: int | None = None
    FileName: str | None = None
    Bitrate: int | None = None
    ProductionYear: int | None = None
    Number: str | None = None
    ChannelNumber: str | None = None
    IndexNumber: int | None = None
    IndexNumberEnd: int | None = None
    ParentIndexNumber: int | None = None
    RemoteTrailers: list[ExternalUrl] = Field(default_factory=list) # MediaUrl
    ProviderIds: dict[str, str] = Field(default_factory=dict)
    IsFolder: bool | None = None
    ParentId: str
    Type: str
    People: list[BaseItemPerson] = Field(default_factory=list)
    Studios: list[NameLongIdPair] = Field(default_factory=list)
    GenreItems: list[NameLongIdPair] = Field(default_factory=list)
    TagItems: list[NameLongIdPair] = Field(default_factory=list)
    ParentLogoItemId: str
    ParentBackdropItemId: str
    ParentBackdropImageTags: list[str] = Field(default_factory=list)
    LocalTrailerCount: int | None = None
    UserData: UserItemDataDto | None = None
    RecursiveItemCount: int | None = None
    ChildCount: int | None = None
    SeasonCount: int | None = None
    SeriesName: str
    SeriesId: str
    SeasonId: str
    SpecialFeatureCount: int | None = None
    DisplayPreferencesId: str | None = None
    Status: str | None = None
    AirDays: list[str] = Field(default_factory=list)
    Tags: list[str] = Field(default_factory=list)
    PrimaryImageAspectRatio: float | None = None
    Artists: list[str] = Field(default_factory=list)
    ArtistItems: list[NameLongIdPair] = Field(default_factory=list) # NameIdPair
    Composers: list[NameLongIdPair] = Field(default_factory=list) # NameIdPair
    Album: str | None = None
    CollectionType: str | None = None
    DisplayOrder: str | None = None
    AlbumId: str | None = None
    AlbumPrimaryImageTag: str | None = None
    SeriesPrimaryImageTag: str | None = None
    AlbumArtist: str | None = None
    AlbumArtists: list[NameLongIdPair] = Field(default_factory=list) # NameIdPair
    SeasonName: str
    MediaStreams: list[MediaStream] = Field(default_factory=list)
    PartCount: int | None = None
    ImageTags: dict[str, str] = Field(default_factory=dict)
    BackdropImageTags: list[str] = Field(default_factory=list)
    ParentLogoImageTag: str
    SeriesStudio: str | None = None
    PrimaryImageItemId: str | None = None
    PrimaryImageTag: str | None = None
    ParentThumbItemId: str | None = None
    ParentThumbImageTag: str | None = None
    Chapters: list[ChapterInfo] = Field(default_factory=list)
    LocationType: str | None = None
    MediaType: str
    EndDate: str | None = None
    LockedFields: list[str] = Field(default_factory=list)
    LockData: bool | None = None
    Width: int | None = None
    Height: int | None = None
    CameraMake: str | None = None
    CameraModel: str | None = None
    Software: str | None = None
    ExposureTime: float | None = None
    FocalLength: float | None = None
    ImageOrientation: str | None = None
    Aperture: float | None = None
    ShutterSpeed: float | None = None
    Latitude: float | None = None
    Longitude: float | None = None
    Altitude: float | None = None
    IsoSpeedRating: int | None = None
    SeriesTimerId: str | None = None
    ChannelPrimaryImageTag: str | None = None
    StartDate: str | None = None
    CompletionPercentage: float | None = None
    IsRepeat: bool | None = None
    IsNew: bool | None = None
    EpisodeTitle: str | None = None
    IsMovie: bool | None = None
    IsSports: bool | None = None
    IsSeries: bool | None = None
    IsLive: bool | None = None
    IsNews: bool | None = None
    IsKids: bool | None = None
    IsPremiere: bool | None = None
    TimerType: str | None = None
    Disabled: bool | None = None
    ManagementId: str | None = None
    TimerId: str | None = None
    # CurrentProgram: str | None # BaseItemDto
    MovieCount: int | None = None
    SeriesCount: int | None = None
    AlbumCount: int | None = None
    SongCount: int | None = None
    MusicVideoCount: int | None = None
    Subviews: list[str] = Field(default_factory=list)
    ListingsProviderId: str | None = None
    ListingsChannelId: str | None = None
    ListingsPath: str | None = None
    ListingsId: str | None = None
    ListingsChannelName: str | None = None
    ListingsChannelNumber: str | None = None
    AffiliateCallSign: str | None = None

class QueryResult_BaseItemDto(BaseModel):
    """Emby 搜索结果模型"""
    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int
