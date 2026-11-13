"""Sonarr Webhook 回调数据模型（手写维护，与 OpenAPI REST 模型解耦）。"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping
from models.radarr_webhook import (
    Languages,
    MediaCover,
    MediaInfoDto,
    WebhookBase,
    customFormatInfoDto,
)


class SonarrSeries(BaseModel):
    """Sonarr Webhook 剧集模型。"""

    id: int
    title: str
    year: int
    titleSlug: str
    path: str
    tvMazeId: int
    tvdbId: int
    tmdbId: int
    imdbId: str
    type: str
    overview: str | None = None
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    images: list[MediaCover] = Field(default_factory=list)
    originalLanguage: Languages | None = None

    @field_validator("genres", mode="before")
    @classmethod
    def validate_genres(cls, value: Any) -> Any:
        """按全局类型映射表转换剧集类型名称。"""
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]


class SonarrEpisode(BaseModel):
    """Sonarr Webhook 单集模型。"""

    id: int
    seasonNumber: int
    episodeNumber: int
    title: str
    overview: str | None = None
    airDate: date
    airDateUtc: datetime | None = None
    seriesId: int
    tvdbId: int
    finaleType: str | None = None


class FileInfoDto(BaseModel):
    """Sonarr Webhook 剧集文件模型。"""

    id: int
    relativePath: str
    path: str
    quality: str
    qualityVersion: int
    releaseGroup: str
    sceneName: str | None = None
    size: int
    dateAdded: datetime
    languages: list[Languages] = Field(default_factory=list)
    mediaInfo: MediaInfoDto | None = None
    sourcePath: str | None = None


class SonarrRelease(BaseModel):
    """Sonarr 发布版本信息。"""

    quality: str | None = None
    qualityVersion: int | None = None
    releaseGroup: str | None = None
    releaseTitle: str | None = None
    indexer: str | None = None
    size: int | None = None
    customFormatScore: int | None = None
    customFormats: list[str] = Field(default_factory=list)
    languages: list[Languages] = Field(default_factory=list)
    releaseType: str | None = None
    indexerFlags: list[str] = Field(default_factory=list)


class RenamedEpisodeFiles(BaseModel):
    """重命名剧集文件记录。"""

    previousRelativePath: str
    previousPath: str


class DownloadInfo(BaseModel):
    """下载任务摘要信息。"""

    quality: str
    qualityVersion: int
    title: str | None = None
    indexer: str | None = None
    size: int


class SonarrWebhookGrabPayload(WebhookBase[Literal["Grab"]]):
    """Sonarr Grab 事件载荷。"""

    eventType: Literal["Grab"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    release: SonarrRelease
    downloadClient: str
    downloadClientType: str
    downloadId: str
    customFormatInfo: customFormatInfoDto


class SonarrWebhookDownloadPayload(WebhookBase[Literal["Download"]]):
    """Sonarr Download / Import 事件载荷。"""

    eventType: Literal["Download"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    episodeFile: FileInfoDto | None = None
    episodeFiles: list[FileInfoDto] = Field(default_factory=list)
    release: SonarrRelease
    isUpgrade: bool | None = None
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    customFormatInfo: customFormatInfoDto | None = None
    deletedFiles: list[FileInfoDto] = Field(default_factory=list)

    sourcePath: str | None = None
    destinationPath: str | None = None


class SonarrWebhookEpisodeDeletePayload(WebhookBase[Literal["EpisodeFileDelete"]]):
    """Sonarr EpisodeFileDelete 事件载荷。"""

    eventType: Literal["EpisodeFileDelete"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    episodeFile: FileInfoDto
    deleteReason: str


class SonarrWebhookSeriesAddPayload(WebhookBase[Literal["SeriesAdd"]]):
    """Sonarr SeriesAdd 事件载荷。"""

    eventType: Literal["SeriesAdd"]
    series: SonarrSeries


class SonarrWebhookSeriesDeletePayload(WebhookBase[Literal["SeriesDelete"]]):
    """Sonarr SeriesDelete 事件载荷。"""

    eventType: Literal["SeriesDelete"]
    series: SonarrSeries
    deletedFiles: bool


class SonarrWebhookRenamePayload(WebhookBase[Literal["Rename"]]):
    """Sonarr Rename 事件载荷。"""

    eventType: Literal["Rename"]
    series: SonarrSeries
    renamedEpisodeFiles: list[RenamedEpisodeFiles] = Field(default_factory=list)


class SonarrWebhookHealthPayload(WebhookBase[Literal["Health", "HealthRestored"]]):
    """Sonarr Health / HealthRestored 事件载荷。"""

    eventType: Literal["Health", "HealthRestored"]
    level: str
    message: str
    type: str
    wikiUrl: str | None = None


class SonarrWebhookApplicationUpdatePayload(WebhookBase[Literal["ApplicationUpdate"]]):
    """Sonarr ApplicationUpdate 事件载荷。"""

    eventType: Literal["ApplicationUpdate"]
    message: str
    previousVersion: str
    newVersion: str


class SonarrWebhookManualInteractionPayload(
    WebhookBase[Literal["ManualInteractionRequired"]]
):
    """Sonarr ManualInteractionRequired 事件载荷。"""

    eventType: Literal["ManualInteractionRequired"]
    series: SonarrSeries | None = None  # 未知剧集时为 None
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    downloadInfo: DownloadInfo
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str
    downloadStatus: str
    customFormatInfo: customFormatInfoDto


class SonarrWebhookTestPayload(WebhookBase[Literal["Test"]]):
    """Sonarr Test 事件载荷。"""

    eventType: Literal["Test"]


SonarrPayload = Annotated[
    SonarrWebhookGrabPayload
    | SonarrWebhookDownloadPayload
    | SonarrWebhookEpisodeDeletePayload
    | SonarrWebhookSeriesAddPayload
    | SonarrWebhookSeriesDeletePayload
    | SonarrWebhookRenamePayload
    | SonarrWebhookHealthPayload
    | SonarrWebhookApplicationUpdatePayload
    | SonarrWebhookManualInteractionPayload
    | SonarrWebhookTestPayload,
    Field(discriminator="eventType"),
]
