from typing import TypeAlias

from pydantic import BaseModel, Field
from models.emby import ExternalUrl, NameLongIdPair

NameGuidPair: TypeAlias = NameLongIdPair

class UserConfiguration(BaseModel):
    """Jellyfin 用户配置模型"""
    AudioLanguagePreference: str | None = None
    PlayDefaultAudioTrack: bool
    SubtitleLanguagePreference: str | None = None
    DisplayMissingEpisodes: bool
    GroupedFolders: list[str] = Field(default_factory=list)
    SubtitleMode: str
    DisplayCollectionsView: bool
    EnableLocalPassword: bool
    OrderedViews: list[str] = Field(default_factory=list)
    LatestItemsExcludes: list[str] = Field(default_factory=list)
    MyMediaExcludes: list[str] = Field(default_factory=list)
    HidePlayedInLatest: bool
    RememberAudioSelections: bool
    RememberSubtitleSelections: bool
    EnableNextEpisodeAutoPlay: bool
    CastReceiverId: str | None = None

class AccessSchedule(BaseModel):
    """Jellyfin 访问时间表模型"""
    Id: int
    UserId: str
    DayOfWeek: str
    StartHour: float
    EndHour: float

class UserPolicy(BaseModel):
    """Jellyfin 用户策略模型"""
    IsAdministrator: bool = False
    IsHidden: bool = True
    EnableCollectionManagement: bool = False
    EnableSubtitleManagement: bool = False
    EnableLyricManagement: bool = False
    IsDisabled: bool = False
    MaxParentalRating: int | None = None
    MaxParentalSubRating: int | None = None
    BlockedTags: list[str] = Field(default_factory=list)
    AllowedTags: list[str] = Field(default_factory=list)
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
    ForceRemoteSourceTranscoding: bool = False
    EnableContentDeletion: bool = False
    EnableContentDeletionFromFolders: list[str] = Field(default_factory=list)
    EnableContentDownloading: bool = False
    EnableSyncTranscoding: bool = False
    EnableMediaConversion: bool = False
    EnabledDevices: list[str] = Field(default_factory=list)
    EnableAllDevices: bool = True
    EnabledChannels: list[str] = Field(default_factory=list)
    EnableAllChannels: bool = True
    EnabledFolders: list[str] = Field(default_factory=list)
    EnableAllFolders: bool = True
    InvalidLoginAttemptCount: int = 0
    LoginAttemptsBeforeLockout: int = 0
    MaxActiveSessions: int = 0
    EnablePublicSharing: bool = False
    BlockedMediaFolders: list[str] = Field(default_factory=list)
    BlockedChannels: list[str] = Field(default_factory=list)
    RemoteClientBitrateLimit: int = 0
    AuthenticationProviderId: str | None = None
    PasswordResetProviderId: str | None = None
    SyncPlayAccess: str | None = None

class UserDto(BaseModel):
    """Jellyfin 用户模型""" 
    Name: str | None = None
    ServerId: str | None = None
    ServerName: str | None = None
    Id: str
    PrimaryImageTag: str | None = None
    HasPassword: bool
    HasConfiguredPassword: bool
    HasConfiguredEasyPassword: bool
    EnableAutoLogin: bool | None = None
    LastLoginDate: str | None = None # datetime
    LastActivityDate: str | None = None # datetime
    Configuration: UserConfiguration
    Policy: UserPolicy
    PrimaryImageAspectRatio: float | None = None

class MediaStream(BaseModel):
    """Jellyfin 媒体流模型"""
    Codec: str | None = None
    CodecTag: str | None = None
    Language: str | None = None
    ColorRange: str | None = None
    ColorSpace: str | None = None
    ColorTransfer: str | None = None
    ColorPrimaries: str | None = None
    DvVersionMajor: int | None = None
    DvVersionMinor: int | None = None
    DvProfile: int | None = None
    DvLevel: int | None = None
    RpuPresentFlag: int | None = None
    ElPresentFlag: int | None = None
    BlPresentFlag: int | None = None
    DvBlSignalCompatibilityId: int | None = None
    Rotation: int | None = None
    Comment: str | None = None
    TimeBase: str | None = None
    CodecTimeBase: str | None = None
    Title: str | None = None
    Hdr10PlusPresentFlag: bool | None = None
    VideoRange: str = "Unknown"
    VideoRangeType: str = "Unknown"
    VideoDoViTitle: str | None = None
    AudioSpatialFormat: str = "None"
    LocalizedUndefined: str | None = None
    LocalizedDefault: str | None = None
    LocalizedForced: str | None = None
    LocalizedExternal: str | None = None
    LocalizedHearingImpaired: str | None = None
    DisplayTitle: str | None = None
    NalLengthSize: str | None = None
    IsInterlaced: bool
    IsAVC: bool | None = None
    ChannelLayout: str | None = None
    BitRate: int | None = None
    BitDepth: int | None = None
    RefFrames: int | None = None
    PacketLength: int | None = None
    Channels: int | None = None
    SampleRate: int | None = None
    IsDefault: bool
    IsForced: bool
    IsHearingImpaired: bool
    Height: int | None = None
    Width: int | None = None
    AverageFrameRate: float | None = None
    RealFrameRate: float | None = None
    ReferenceFrameRate: float | None = None
    Profile: str | None = None
    Type: str
    AspectRatio: str | None = None
    Index: int
    Score: int | None = None
    IsExternal: bool
    DeliveryMethod: str | None = None
    DeliveryUrl: str | None = None
    IsExternalUrl: bool | None = None
    IsTextSubtitleStream: bool
    SupportsExternalStream: bool
    Path: str | None = None
    PixelFormat: str | None = None
    Level: float | None = None
    IsAnamorphic: bool | None = None

class MediaAttachment(BaseModel):
    """Jellyfin 媒体附件模型"""
    Codec: str | None = None
    CodecTag: str | None = None
    Comment: str | None = None
    Index: int | None = None
    FileName: str | None = None
    MimeType: str | None = None
    DeliveryUrl: str | None = None

class MediaSourceInfo(BaseModel):
    """Jellyfin 媒体源模型"""
    Protocol: str
    Id: str | None = None
    Path: str | None = None
    EncoderPath: str | None = None
    EncoderProtocol: str | None = None
    Type: str
    Container: str | None = None
    Size: int | None = None
    Name: str | None = None
    IsRemote: bool
    ETag: str | None = None
    RunTimeTicks: int | None = None
    ReadAtNativeFramerate: bool
    IgnoreDts: bool
    IgnoreIndex: bool
    GenPtsInput: bool
    SupportsTranscoding: bool
    SupportsDirectStream: bool
    SupportsDirectPlay: bool
    IsInfiniteStream: bool
    UseMostCompatibleTranscodingProfile: bool = False
    RequiresOpening: bool
    OpenToken: str | None = None
    RequiresClosing: bool
    LiveStreamId: str | None = None
    BufferMs: int | None = None
    RequiresLooping: bool
    SupportsProbing: bool
    VideoType: str | None = None
    IsoType: str | None = None
    Video3DFormat: str | None = None
    MediaStreams: list[MediaStream] = Field(default_factory=list)
    MediaAttachments: list[MediaAttachment] = Field(default_factory=list)
    Formats: list[str] = Field(default_factory=list)
    Bitrate: int | None = None
    FallbackMaxStreamingBitrate: int | None = None
    Timestamp: str | None = None
    RequiredHttpHeaders: dict[str, str] = Field(default_factory=dict)
    TranscodingUrl: str | None = None
    TranscodingSubProtocol: str | None = None
    TranscodingContainer: str | None = None
    AnalyzeDurationMs: int | None = None
    DefaultAudioStreamIndex: int | None = None
    DefaultSubtitleStreamIndex: int | None = None
    HasSegments: bool

class ImageBlurHash(BaseModel):
    """Jellyfin 图片模糊哈希模型"""
    Primary: dict[str, str] = Field(default_factory=dict)
    Art: dict[str, str] = Field(default_factory=dict)
    Backdrop: dict[str, str] = Field(default_factory=dict)
    Banner: dict[str, str] = Field(default_factory=dict)
    Logo: dict[str, str] = Field(default_factory=dict)
    Thumb: dict[str, str] = Field(default_factory=dict)
    Disc: dict[str, str] = Field(default_factory=dict)
    Box: dict[str, str] = Field(default_factory=dict)
    Screenshot: dict[str, str] = Field(default_factory=dict)
    Menu: dict[str, str] = Field(default_factory=dict)
    Chapter: dict[str, str] = Field(default_factory=dict)
    BoxRear: dict[str, str] = Field(default_factory=dict)
    Profile: dict[str, str] = Field(default_factory=dict)

class BaseItemPerson(BaseModel):
    """Jellyfin 媒体项人员模型"""
    Name: str | None = None
    Id: str
    Role: str | None = None
    Type: str = "Unknown"
    PrimaryImageTag: str | None = None
    ImageBlurHashes: ImageBlurHash | None = None

class UserItemDataDto(BaseModel):
    """Jellyfin 用户媒体项数据模型"""
    Rating: float | None = None
    PlayedPercentage: float | None = None
    UnplayedItemCount: int | None = None
    PlaybackPositionTicks: int
    PlayCount: int | None = None
    IsFavorite: bool
    Likes: bool | None = None
    LastPlayedDate: str | None = None # datetime
    Played: bool
    Key: str
    ItemId: str

class ChapterInfo(BaseModel):
    """Jellyfin 章节信息模型"""
    StartPositionTicks: int
    Name: str | None = None
    ImagePath: str | None = None
    ImageDateModified: str | None = None # datetime
    ImageTag: str | None = None

class TrickplayInfoDto(BaseModel):
    """Jellyfin Trickplay 信息模型"""
    Width: int
    Height: int
    TileWidth: int
    TileHeight: int
    ThumbnailCount: int
    Interval: int
    Bandwidth: int

class BaseItemDto(BaseModel):
    """Jellyfin 媒体项模型"""
    Name: str
    OriginalTitle: str | None = None
    ServerId: str
    Id: str
    Etag: str | None = None
    SourceType: str | None = None
    PlaylistItemId: str | None = None
    DateCreated: str | None = None # datetime
    DateLastMediaAdded: str | None = None # datetime
    ExtraType: str | None = None
    AirsBeforeSeasonNumber: int | None = None
    AirsAfterSeasonNumber: int | None = None
    AirsBeforeEpisodeNumber: int | None = None
    CanDelete: bool | None = None
    CanDownload: bool | None = None
    HasLyrics: bool | None = None
    HasSubtitles: bool | None = None
    PreferredMetadataLanguage: str | None = None
    PreferredMetadataCountryCode: str | None = None
    Container: str | None = None
    SortName: str | None = None
    ForcedSortName: str | None = None
    Video3DFormat: str | None = None
    PremiereDate: str | None = None # datetime
    ExternalUrls: list[ExternalUrl] = Field(default_factory=list)
    MediaSources: list[MediaSourceInfo] = Field(default_factory=list)
    CriticRating: float | None = None
    ProductionLocations: list[str] = Field(default_factory=list)
    Path: str | None = None
    EnableMediaSourceDisplay: bool | None = None
    OfficialRating: str | None = None
    CustomRating: str | None = None
    ChannelId: str | None = None
    ChannelName: str | None = None
    Overview: str | None = None
    Taglines: list[str] = Field(default_factory=list)
    Genres: list[str] = Field(default_factory=list)
    CommunityRating: float | None = None
    CumulativeRunTimeTicks: int | None = None
    RunTimeTicks: int | None = None
    PlayAccess: str | None = None
    AspectRatio: str | None = None
    ProductionYear: int | None = None
    IsPlaceHolder: bool | None = None
    Number: str | None = None
    ChannelNumber: str | None = None
    IndexNumber: int | None = None
    IndexNumberEnd: int | None = None
    ParentIndexNumber: int | None = None
    RemoteTrailers: list[ExternalUrl] = Field(default_factory=list) # MediaUrl
    ProviderIds: dict[str, str] = Field(default_factory=dict)
    IsHD: bool | None = None
    IsFolder: bool | None = None
    ParentId: str | None = None
    Type: str
    People: list[BaseItemPerson] = Field(default_factory=list)
    Studios: list[NameGuidPair] = Field(default_factory=list)
    GenreItems: list[NameGuidPair] = Field(default_factory=list)
    ParentLogoItemId: str | None = None
    ParentBackdropItemId: str | None = None
    ParentBackdropImageTags: list[str] = Field(default_factory=list)
    LocalTrailerCount: int | None = None
    UserData: UserItemDataDto | None = None
    RecursiveItemCount: int | None = None
    ChildCount: int | None = None
    SeriesName: str | None = None
    SeriesId: str | None = None
    SeasonId: str | None = None
    SpecialFeatureCount: int | None = None
    DisplayPreferencesId: str | None = None
    Status: str | None = None
    AirTime: str | None = None
    AirDays: list[str] = Field(default_factory=list)
    Tags: list[str] = Field(default_factory=list)
    PrimaryImageAspectRatio: float | None = None
    Artists: list[str] = Field(default_factory=list)
    ArtistItems: list[NameGuidPair] = Field(default_factory=list) # NameIdPair
    Album: str | None = None
    CollectionType: str | None = None
    DisplayOrder: str | None = None
    AlbumId: str | None = None
    AlbumPrimaryImageTag: str | None = None
    SeriesPrimaryImageTag: str | None = None
    AlbumArtist: str | None = None
    AlbumArtists: list[NameGuidPair] = Field(default_factory=list) # NameIdPair
    SeasonName: str | None = None
    MediaStreams: list[MediaStream] = Field(default_factory=list)
    VideoType: str | None = None
    PartCount: int | None = None
    MediaSourceCount: int | None = None
    ImageTags: dict[str, str] = Field(default_factory=dict)
    BackdropImageTags: list[str] = Field(default_factory=list)
    ScreenshotImageTags: list[str] = Field(default_factory=list)
    ParentLogoImageTag: str | None = None
    ParentArtItemId: str | None = None
    ParentArtImageTag: str | None = None
    SeriesThumbItemId: str | None = None
    ImageBlurHashes: ImageBlurHash | None = None
    SeriesStudio: str | None = None
    ParentThumbItemId: str | None = None
    ParentThumbImageTag: str | None = None
    ParentPrimaryImageItemId: str | None = None
    ParentPrimaryImageTag: str | None = None
    Chapters: list[ChapterInfo] = Field(default_factory=list)
    Trickplay: dict[str, TrickplayInfoDto | dict[str, TrickplayInfoDto]] = Field(default_factory=dict)
    LocationType: str | None = None
    IsoType: str | None = None
    MediaType: str = "Unknown"
    EndDate: str | None = None # datetime
    LockedFields: list[str] = Field(default_factory=list)
    TrailerCount: int | None = None
    MovieCount: int | None = None
    SeriesCount: int | None = None
    ProgramCount: int | None = None
    EpisodeCount: int | None = None
    SongCount: int | None = None
    AlbumCount: int | None = None
    ArtistCount: int | None = None
    MusicVideoCount: int | None = None
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
    ProgramId: str | None = None
    ChannelPrimaryImageTag: str | None = None
    StartDate: str | None = None # datetime
    CompletionPercentage: float | None = None
    IsRepeat: bool | None = None
    EpisodeTitle: str | None = None
    ChannelType: str | None = None
    Audio: str | None = None
    IsMovie: bool | None = None
    IsSports: bool | None = None
    IsSeries: bool | None = None
    IsLive: bool | None = None
    IsNews: bool | None = None
    IsKids: bool | None = None
    IsPremiere: bool | None = None
    TimerId: str | None = None
    Disabled: bool | None = None
    ManagementId: str | None = None
    TimerId: str | None = None
    NormalizationGain: float | None = None
    # CurrentProgram: str | None # BaseItemDto

class BaseItemDtoQueryResult(BaseModel):
    """Jellyfin 搜索结果模型"""
    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int
    StartIndex: int
