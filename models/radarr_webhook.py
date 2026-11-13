"""Radarr Webhook 回调数据模型（手写维护，与 OpenAPI REST 模型解耦）。"""

from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping

EventT = TypeVar("EventT", bound=str)


class Languages(BaseModel):
    """Radarr Webhook 语言模型。"""

    id: int
    name: str | None = None


class MediaCover(BaseModel):
    """Radarr Webhook 封面图片模型。"""

    coverType: str
    url: str | None = None
    remoteUrl: str | None = None


class CustomFormatResource(BaseModel):
    """Radarr Webhook 自定义格式模型。"""

    id: int
    name: str | None = None
    includeCustomFormatWhenRenaming: bool | None = None
    specifications: list[dict[str, Any]] = Field(default_factory=list)


class customFormatInfoDto(BaseModel):
    """自定义格式汇总信息。"""

    customFormats: list[CustomFormatResource] = Field(default_factory=list)
    customFormatScore: int | None = None

    @property
    def formats_str(self) -> str:
        """将 format 列表输出为逗号分隔字符串。"""
        if not self.customFormats:
            return ""
        return ",".join(_format.name for _format in self.customFormats if _format.name)


class Movie(BaseModel):
    """Radarr Webhook 电影模型。"""

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

    @field_validator("genres", mode="before")
    @classmethod
    def validate_genres(cls, value: Any) -> Any:
        """按全局类型映射表转换电影类型名称。"""
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]


class RemoteMovie(BaseModel):
    """Radarr 远端电影标识模型。"""

    tmdbId: int
    imdbId: str
    title: str
    year: int


class MediaInfoDto(BaseModel):
    """Radarr Webhook 媒体流信息。"""

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
    """Radarr Webhook 电影文件模型。"""

    id: int
    relativePath: str
    path: str
    quality: str
    qualityVersion: int
    releaseGroup: str
    sceneName: str | None = None
    indexerFlags: str
    size: int
    dateAdded: datetime
    languages: list[Languages] = Field(default_factory=list)
    mediaInfo: MediaInfoDto | None = None
    sourcePath: str | None = None


class RadarrRelease(BaseModel):
    """Radarr 发布版本信息。"""

    quality: str | None = None
    qualityVersion: int | None = None
    releaseGroup: str | None = None
    releaseTitle: str | None = None
    indexer: str | None = None
    size: int
    customFormatScore: int | None = None
    customFormats: list[str] = Field(default_factory=list)
    languages: list[Languages] = Field(default_factory=list)
    indexerFlags: list[str] = Field(default_factory=list)


class RenamedMovieFiles(BaseModel):
    """重命名电影文件记录。"""

    previousRelativePath: str
    previousPath: str


class DownloadInfo(BaseModel):
    """下载任务摘要信息。"""

    quality: str
    qualityVersion: int
    title: str
    size: int


class WebhookBase(BaseModel, Generic[EventT]):
    """Arr Webhook 通用基类。"""

    eventType: EventT
    instanceName: str | None = None
    applicationUrl: str | None = None


class RadarrWebhookGrabPayload(WebhookBase[Literal["Grab"]]):
    """Radarr Grab 事件载荷。"""

    eventType: Literal["Grab"]
    movie: Movie
    remoteMovie: RemoteMovie
    release: RadarrRelease
    downloadClient: str
    downloadClientType: str
    downloadId: str
    customFormatInfo: customFormatInfoDto


class RadarrWebhookDownloadPayload(WebhookBase[Literal["Download"]]):
    """Radarr Download / Import 事件载荷。"""

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
    """Radarr MovieAdded 事件载荷。"""

    eventType: Literal["MovieAdded"]
    movie: Movie
    addMethod: str


class RadarrWebhookMovieFileDeletePayload(WebhookBase[Literal["MovieFileDelete"]]):
    """Radarr MovieFileDelete 事件载荷。"""

    eventType: Literal["MovieFileDelete"]
    movie: Movie
    movieFile: FileInfoDto
    deleteReason: str


class RadarrWebhookMovieDeletePayload(WebhookBase[Literal["MovieDelete"]]):
    """Radarr MovieDelete 事件载荷。"""

    eventType: Literal["MovieDelete"]
    movie: Movie
    deletedFiles: bool
    movieFolderSize: int | None = None


class RadarrWebhookRenamePayload(WebhookBase[Literal["Rename"]]):
    """Radarr Rename 事件载荷。"""

    eventType: Literal["Rename"]
    movie: Movie
    renamedMovieFiles: list[RenamedMovieFiles] = Field(default_factory=list)


class RadarrWebhookHealthPayload(WebhookBase[Literal["Health", "HealthRestored"]]):
    """Radarr Health / HealthRestored 事件载荷。"""

    eventType: Literal["Health", "HealthRestored"]
    level: str
    message: str
    type: str
    wikiUrl: str | None = None


class RadarrWebhookApplicationUpdatePayload(WebhookBase[Literal["ApplicationUpdate"]]):
    """Radarr ApplicationUpdate 事件载荷。"""

    eventType: Literal["ApplicationUpdate"]
    message: str
    previousVersion: str
    newVersion: str


class RadarrWebhookManualInteractionPayload(
    WebhookBase[Literal["ManualInteractionRequired"]]
):
    """Radarr ManualInteractionRequired 事件载荷。"""

    eventType: Literal["ManualInteractionRequired"]
    movie: Movie | None = None
    downloadInfo: DownloadInfo
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str
    downloadStatus: str
    customFormatInfo: customFormatInfoDto


class RadarrWebhookTestPayload(WebhookBase[Literal["Test"]]):
    """Radarr Test 事件载荷。"""

    eventType: Literal["Test"]


RadarrPayload = Annotated[
    RadarrWebhookGrabPayload
    | RadarrWebhookDownloadPayload
    | RadarrWebhookAddedPayload
    | RadarrWebhookMovieFileDeletePayload
    | RadarrWebhookMovieDeletePayload
    | RadarrWebhookRenamePayload
    | RadarrWebhookHealthPayload
    | RadarrWebhookApplicationUpdatePayload
    | RadarrWebhookManualInteractionPayload
    | RadarrWebhookTestPayload,
    Field(discriminator="eventType"),
]
