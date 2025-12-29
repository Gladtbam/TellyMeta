# from __future__ import annotations

from datetime import datetime, time
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping


EventT = TypeVar("EventT", bound=str)

class Languages(BaseModel):
    """Radarr 语言模型"""
    id: int
    name: str | None = None

class AlternativeTitleResource(BaseModel):
    """Radarr 电影别名模型"""
    id: int | None = None
    sourceType: str
    movieMetadataId: int
    title: str | None = None
    cleanTitle: str | None = None

class MediaCover(BaseModel):
    """Radarr 电影图片模型"""
    coverType: str
    url: str | None = None
    remoteUrl: str | None = None

class AddMovieOptions(BaseModel):
    """Radarr 添加电影选项模型"""
    ignoreEpisodesWithFiles: bool
    ignoreEpisodesWithoutFiles: bool
    monitor: str
    searchForMovie: bool
    addMethod: str

class RatingChild(BaseModel):
    """电影评分模型"""
    value: float | None = None
    votes: int | None = None
    type: str | None = None

class Ratings(BaseModel):
    """电影评分模型"""
    imdb: RatingChild | None = None
    tmdb: RatingChild | None = None
    rottenTomatoes: RatingChild | None = None
    metacritic: RatingChild | None = None
    trakt: RatingChild | None = None

class Quality(BaseModel):
    """Radarr 质量模型"""
    id: int
    name: str | None = None
    source: str | None = None
    resolution: int | None = None
    modifier: str | None = None

class Revision(BaseModel):
    """Radarr 质量修订模型"""
    version: int
    real: bool
    isRepack: bool

class QualityModel(BaseModel):
    """Radarr 质量模型"""
    quality: Quality | None = None
    revision: Revision | None = None

class SelectOptions(BaseModel):
    """Radarr 选择选项模型"""
    name: str | None = None
    value: str
    order: int
    hint: str | None = None
    dividerAfter: bool | None = None

class FieldFieldResource(BaseModel):
    """Radarr 字段模型"""
    order: int
    name: str | None = None
    label: str | None = None
    unit: str | None = None
    helpText: str | None = None
    helpTextWarning: str | None = None
    helpLink: str | None = None
    # value: str | None = None
    type: str | None = None
    advanced: bool
    selectOptions: list[SelectOptions] = Field(default_factory=list)
    selectOptionsProviderAction: str | None = None
    section: str | None = None
    hidden: str | None = None
    privacy: str
    placeholder: str | None = None
    isFloat: bool

class CustomFormatSpecificationSchema(BaseModel):
    """Radarr 规格模型"""
    id: int
    name: str | None = None
    implementation: str | None = None
    implementationName: str | None = None
    infoLink: str | None = None
    negate: bool
    required: bool
    fields: list[FieldFieldResource] = Field(default_factory=list)
    presets: list[str] = Field(default_factory=list)

class CustomFormatResource(BaseModel):
    """Radarr 自定义格式模型"""
    id: int
    name: str | None = None
    includeCustomFormatWhenRenaming: bool | None = None
    specifications: list[CustomFormatSpecificationSchema] = Field(default_factory=list)

class MediaInfoResource(BaseModel):
    """Radarr 媒体信息模型"""
    id: int | None = None
    audioBitrate: int
    audioChannels: float
    audioCodec: str | None = None
    audioLanguage: str | None = None
    audioStreamCount: int
    videoBitDepth: int
    videoBitrate: int
    videoCodec: str | None = None
    videoFps: float
    videoDynamicRange: str | None = None
    videoDynamicRangeType: str | None = None
    resolution: str | None = None
    runTime: int | str | None = None
    scanType: str | None = None
    subtitles: str | None = None

class MovieFileResource(BaseModel):
    """Radarr 电影文件模型"""
    id: int
    movieId: int
    relativePath: str | None = None
    path: str | None = None
    size: int
    dateAdded: str
    sceneName: str | None = None
    releaseGroup: str | None = None
    edition: str | None = None
    languages: list[Languages] = Field(default_factory=list)
    quality: QualityModel | None = None
    customFormats: list[CustomFormatResource] = Field(default_factory=list)
    customFormatScore: int | None = None
    indexerFlags: int | None = None
    mediaInfo: MediaInfoResource | None = None
    originalFilePath: str | None = None
    qualityCutoffNotMet: bool

class MovieCollectionResource(BaseModel):
    """Radarr 电影合集模型"""
    title: str | None = None
    tmdbId: int

class MovieStatisticsResource(BaseModel):
    """Radarr 电影统计模型"""
    movieFileCount: int
    sizeOnDisk: int
    releaseGroups: list[str] = Field(default_factory=list)

class MovieResource(BaseModel):
    """Radarr 电影模型"""
    id: int | None = None
    title: str | None = None
    originalTitle: str | None = None
    originalLanguage: Languages | None = None
    alternateTitles: list[AlternativeTitleResource] = Field(default_factory=list)
    secondaryYear: int | None = None
    secondaryYearSourceId: int
    sortTitle: str | None = None
    sizeOnDisk: int | None = None
    status: str
    overview: str | None = None
    inCinemas: str | None = None
    physicalRelease: str | None = None
    digitalRelease: str | None = None
    releaseDate: str | None = None
    physicalReleaseNote: str | None = None
    images: list[MediaCover] = Field(default_factory=list)
    website: str | None = None
    remotePoster: str | None = None
    year: int
    youTubeTrailerId: str | None = None
    studio: str | None = None
    path: str | None = None
    qualityProfileId: int
    hasFile: bool | None = None
    movieFileId: int
    monitored: bool
    minimumAvailability: str
    isAvailable: bool
    folderName: str | None = None
    runtime: int | str | None = None
    cleanTitle: str | None = None
    imdbId: str | None = None
    tmdbId: int
    titleSlug: str | None = None
    rootFolderPath: str | None = None
    folder: str | None = None
    certification: str | None = None
    genres: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)
    added: str | None = None
    addOptions: AddMovieOptions | None = None
    ratings: Ratings | None = None
    movieFile: MovieFileResource | None = None
    collection: MovieCollectionResource | None = None
    popularity: float
    lastSearchTime: str | None = None
    statistics: MovieStatisticsResource | None = None

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]

class UnmappedFolder(BaseModel):
    """未映射文件夹模型"""
    name: str | None = None
    path: str | None = None
    relativePath: str | None = None

class RootFolderResource(BaseModel):
    """根文件夹模型"""
    accessible: bool
    freeSpace: int | None = None
    id: int
    path: str | None = None
    unmappedFolders: list[UnmappedFolder] = Field(default_factory=list)

    @property
    def free_space_human(self) -> str:
        """自动将 freeSpace 转换为最合适的 1024 进制单位（B, KiB, MiB, GiB, TiB）"""
        if self.freeSpace == 0 or not self.freeSpace:
            return "0.00 B"
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        size = float(self.freeSpace)
        i = 0
        while size >= 1024.0 and i < len(units) - 1:
            size /= 1024.0
            i += 1
        return f"{size:.2f} {units[i]}"

class ProfileFormatItemResource(BaseModel):
    """Radarr 质量配置文件格式项模型"""
    format: int
    id: int | None = None
    name: str | None = None
    score: int

class QualityProfileQualityItemResource(BaseModel):
    """Radarr 质量配置文件质量项模型"""
    allowed: bool
    id: int | None = None
    items: list['QualityProfileQualityItemResource'] = Field(default_factory=list)
    name: str | None = None
    quality: Quality | None = None
QualityProfileQualityItemResource.model_rebuild()

class QualityProfileResource(BaseModel):
    """Radarr 质量配置文件模型"""
    cutoff: int
    cutoffFormatScore: int
    formatItems: list[ProfileFormatItemResource] = Field(default_factory=list)
    id: int
    items: list[QualityProfileQualityItemResource] = Field(default_factory=list)
    minFormatScore: int
    minUpgradeFormatScore: int
    name: str | None = None
    upgradeAllowed: bool
    language: Languages | None = None

    def to_dict(self) -> dict[int, str]:
        """将质量配置文件转换为字典形式，键为 id，值为 name。"""
        return {self.id: self.name if self.name else ""}

#====================================================
#      RadarrWebHook
#====================================================
class customFormatInfoDto(BaseModel):
    customFormats: list[CustomFormatResource] = Field(default_factory=list)
    customFormatScore: int | None = None

    @property
    def formats_str(self):
        """将 format 列表 输出 字符串"""
        if not self.customFormats:
            return ""
        return ",".join(_format.name for _format in self.customFormats if _format.name)

class Movie(BaseModel):
    """Radarr movie 模型"""
    id: int
    title: str
    year: int
    releaseDate: str
    folderPath: str
    tmdbId: int
    imdbId: str
    overview: str
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    images: list[MediaCover] = Field(default_factory=list)
    originalLanguage: Languages

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]

class RemoteMovie(BaseModel):
    """Radarr remote"""
    tmdbId: int
    imdbId: str
    title: str
    year: int

class MediaInfoDto(BaseModel):
    """Radarr 媒体信息"""
    audioChannels: float
    audioCodec: str
    audioLanguage: list[str] = Field(default_factory=list)
    height: int
    width: int
    subtitles: list[str] = Field(default_factory=list)
    videoCodec: str | None = None
    videoDynamicRange: str | None = None
    videoDynamicRangeType: str | None = None

class FileInfoDto(BaseModel):
    """Radarr 文件模型"""
    id: int
    relativePath: str
    path: str
    quality: str
    qualityVersion: int
    releaseGroup: str
    sceneName: str
    indexerFlags: str
    size: int
    dateAdded: datetime
    languages: list[Languages] = Field(default_factory=list)
    mediaInfo: MediaInfoDto | None = None
    sourcePath: str| None = None

class RadarrRelease(BaseModel):
    """release"""
    quality: str
    qualityVersion: int
    releaseGroup: str
    releaseTitle: str
    indexer: str
    size: str
    customFormatScore: int | None = None
    customFormats: list[str] = Field(default_factory=list)
    languages: list[Languages] = Field(default_factory=list)
    indexerFlags: list[str] = Field(default_factory=list)

class RenamedMovieFiles(BaseModel):
    previousRelativePath: str
    previousPath: str

class DownloadInfo(BaseModel):
    quality: str
    qualityVersion: int
    title: str
    size: int

class WebhookBase(BaseModel, Generic[EventT]):
    eventType: EventT
    instanceName: str | None = None
    applicationUrl: str | None = None

class RadarrWebhookGrabPayload(WebhookBase[Literal["Grab"]]):
    eventType: Literal["Grab"]
    movie: Movie
    remoteMovie: RemoteMovie
    release: RadarrRelease
    downloadClient: str
    downloadClientType: str
    downloadId: str
    customFormatInfo: customFormatInfoDto

class RadarrWebhookDownloadPayload(WebhookBase[Literal["Download"]]):
    """
        WebhookImportPayload
    """
    eventType: Literal["Download"]
    movie: Movie
    remoteMovie: RemoteMovie
    movieFile: FileInfoDto
    release: RadarrRelease
    isUpgrade: bool
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str
    customFormatInfo: customFormatInfoDto

class RadarrWebhookAddedPayload(WebhookBase[Literal["MovieAdded"]]):
    eventType: Literal["MovieAdded"]
    movie: Movie
    addMethod: str

class RadarrWebhookMovieFileDeletePayload(WebhookBase[Literal["MovieFileDelete"]]):
    eventType: Literal["MovieFileDelete"]
    movie: Movie
    movieFile: FileInfoDto
    deleteReason: str

class RadarrWebhookMovieDeletePayload(WebhookBase[Literal["MovieDelete"]]):
    eventType: Literal["MovieDelete"]
    movie: Movie
    deletedFiles: bool
    movieFolderSize: int | None = None

class RadarrWebhookRenamePayload(WebhookBase[Literal["Rename"]]):
    eventType: Literal["Rename"]
    movie: Movie
    renamedMovieFiles: list[RenamedMovieFiles] = Field(default_factory=list)

class RadarrWebhookHealthPayload(WebhookBase[Literal["Health", "HealthRestored"]]):
    eventType: Literal["Health", "HealthRestored"]
    level: str
    message: str
    type: str
    wikiUrl: str | None = None

class RadarrWebhookApplicationUpdatePayload(WebhookBase[Literal["ApplicationUpdate"]]):
    eventType: Literal["ApplicationUpdate"]
    message: str
    previousVersion: str
    newVersion: str

class RadarrWebhookManualInteractionPayload(WebhookBase[Literal["ManualInteractionRequired"]]):
    eventType: Literal["ManualInteractionRequired"]
    movie: Movie
    downloadInfo: DownloadInfo
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str
    downloadStatus: str
    # downloadStatusMessages: list[dict] = Field(default_factory=list)
    customFormatInfo: customFormatInfoDto
    # release: RadarrRelease

class RadarrWebhookTestPayload(WebhookBase[Literal["Test"]]):
    eventType: Literal["Test"]

RadarrPayload = Annotated[
    RadarrWebhookGrabPayload |
    RadarrWebhookDownloadPayload |
    RadarrWebhookAddedPayload |
    RadarrWebhookMovieFileDeletePayload |
    RadarrWebhookMovieDeletePayload |
    RadarrWebhookRenamePayload |
    RadarrWebhookHealthPayload |
    RadarrWebhookApplicationUpdatePayload |
    RadarrWebhookManualInteractionPayload |
    RadarrWebhookTestPayload,
    Field(discriminator="eventType")
]
