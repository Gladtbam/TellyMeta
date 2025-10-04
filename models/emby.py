from pydantic import BaseModel


class EmbyItem(BaseModel):
    """Emby Webhook Item 模型"""
    Name: str
    Id: str
    Path: str | None = None

class EmbyPayload(BaseModel):
    """Emby Webhook 接收数据模型"""
    Event: str
    Item: EmbyItem | None = None

class EmbyUserPolicy(BaseModel):
    """Emby 用户策略模型"""
    IsAdministrator: bool
    IsHidden: bool
    IsHiddenRemotely: bool
    IsHiddenFromUnusedDevices: bool
    IsDisabled: bool
    LockedOutDate: int
    AllowTagOrRating: bool
    BlockedTags: list[str] | None
    IsTagBlockingModeInclusive: bool
    IncludeTags: list[str] | None
    EnableUserPreferenceAccess: bool
    AccessSchedules: list[str] | None
    BlockUnratedItems: list[str] | None
    EnableRemoteControlOfOtherUsers: bool
    EnableSharedDeviceControl: bool
    EnableRemoteAccess: bool
    EnableLiveTvManagement: bool
    EnableLiveTvAccess: bool
    EnableMediaPlayback: bool
    EnableAudioPlaybackTranscoding: bool
    EnableVideoPlaybackTranscoding: bool
    EnablePlaybackRemuxing: bool
    EnableContentDeletion: bool
    RestrictedFeatures: list[str] | None
    EnableContentDeletionFromFolders: list[str] | None
    EnableContentDownloading: bool
    EnableSubtitleDownloading: bool
    EnableSubtitleManagement: bool
    EnableSyncTranscoding: bool
    EnableMediaConversion: bool
    EnabledChannels: list[str] | None
    EnableAllChannels: bool
    EnabledFolders: list[str] | None
    EnableAllFolders: bool
    InvalidLoginAttemptCount: int
    EnablePublicSharing: bool
    RemoteClientBitrateLimit: int
    ExcludedSubFolders: list[str] | None
    SimultaneousStreamLimit: int
    EnabledDevices: list[str] | None
    EnableAllDevices: bool
    AllowCameraUpload: bool
    AllowSharingPersonalItems: bool
    AuthenticationProviderId: str | None

class EmbyUserConfiguration(BaseModel):
    """Emby 用户配置模型"""
    PlayDefaultAudioTrack: bool
    DisplayMissingEpisodes: bool
    SubtitleMode: str
    OrderedViews: list[str] | None
    LatestItemsExcludes: list[str] | None
    MyMediaExcludes: list[str] | None
    HidePlayedInLatest: bool
    HidePlayedInMoreLikeThis: bool
    HidePlayedInSuggestions: bool
    RememberAudioSelections: bool
    RememberSubtitleSelections: bool
    EnableNextEpisodeAutoPlay: bool
    ResumeRewindSeconds: int
    IntroSkipMode: str
    EnableLocalPassword: bool

class EmbyUser(BaseModel):
    """Emby 用户模型"""
    Id: str
    Name: str
    ServerId: str
    Prefix: str
    HasPassword: bool
    HasConfiguredPassword: bool
    Configuration: EmbyUserConfiguration
    Policy: EmbyUserPolicy
    HasConfiguredEasyPassword: bool

class EmbySetUserPolicy(BaseModel):
    """Emby 设置用户策略模型"""
    IsAdministrator: bool | None = False
    IsHidden: bool | None = True
    IsHiddenRemotely: bool | None = True
    IsHiddenFromUnusedDevices: bool | None = True
    IsDisabled: bool | None = False
    # LockedOutDate: int | None
    # MaxParentalRating: int | None
    # AllowTagOrRating: bool | None
    BlockedTags: list[str] | None = None
    IsTagBlockingModeInclusive: bool | None = False
    IncludeTags: list[str] | None = None
    EnableUserPreferenceAccess: bool | None = True
    AccessSchedules: list[dict] | None = None
    BlockUnratedItems: list[str] | None = None
    EnableRemoteControlOfOtherUsers: bool | None = False
    EnableSharedDeviceControl: bool | None = False
    EnableRemoteAccess: bool | None = True
    EnableLiveTvManagement: bool | None = False
    EnableLiveTvAccess: bool | None = True
    EnableMediaPlayback: bool | None = True
    EnableAudioPlaybackTranscoding: bool | None = False
    EnableVideoPlaybackTranscoding: bool | None = False
    EnablePlaybackRemuxing: bool | None = True
    EnableContentDeletion: bool | None = False
    RestrictedFeatures: list[str] | None = None
    EnableContentDeletionFromFolders: list[str] | None = None
    EnableContentDownloading: bool | None = False
    EnableSubtitleDownloading: bool | None = False
    EnableSubtitleManagement: bool | None = False
    EnableSyncTranscoding: bool | None = False
    EnableMediaConversion: bool | None = False
    EnabledChannels: list[str] | None = None
    EnableAllChannels: bool | None = True
    EnabledFolders: list[str] | None = None
    EnableAllFolders: bool | None = True
    InvalidLoginAttemptCount: int | None = 0
    EnablePublicSharing: bool | None = False
    BlockedMediaFolders: list[str] | None = None
    RemoteClientBitrateLimit: int | None = 0
    # AuthenticationProviderId: str | None
    ExcludedSubFolders: list[str] | None = None
    SimultaneousStreamLimit: int | None = 0
    EnabledDevices: list[str] | None = None
    EnableAllDevices: bool | None = True
    AllowCameraUpload: bool | None = False
    AllowSharingPersonalItems: bool | None = False
