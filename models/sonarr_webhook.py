"""Sonarr Webhook 回调数据模型（基于上游 Sonarr Webhook 源码维护，兼容 Python 3.15 与 extra='allow'）。"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from models.radarr_webhook import (
    ArrWebhookBaseModel,
    CustomFormatInfoDto,
    DownloadStatusMessage,
    Languages,
    MediaCover,
    MediaInfoDto,
    WebhookBase,
    normalize_genres,
)


class SonarrSeries(ArrWebhookBaseModel):
    """Sonarr Webhook 剧集信息（对应 ``WebhookSeries``）。"""

    id: int
    title: str
    titleSlug: str | None = None
    path: str
    tvdbId: int = 0
    tvMazeId: int = 0
    tmdbId: int = 0
    imdbId: str | None = None
    malIds: list[int] = Field(default_factory=list)
    aniListIds: list[int] = Field(default_factory=list)
    # SeriesTypes 枚举：standard / daily / anime
    type: str = "standard"
    year: int = 0
    # 上游 WebhookSeries 不直接下发 overview，保留可选字段以兼容 NFO Worker 及旧负载
    overview: str | None = None
    genres: list[str] = Field(default_factory=list)
    images: list[MediaCover] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    originalLanguage: Languages | None = None
    originalCountry: str | None = None

    @field_validator("genres", mode="before")
    @classmethod
    def validate_genres(cls, value: Any) -> Any:
        """按全局类型映射表转换剧集类型名称。"""
        return normalize_genres(value)


class SonarrEpisode(ArrWebhookBaseModel):
    """Sonarr Webhook 单集信息（对应 ``WebhookEpisode``）。"""

    id: int
    episodeNumber: int
    seasonNumber: int
    title: str | None = None
    overview: str | None = None
    airDate: date | None = None
    airDateUtc: datetime | None = None
    seriesId: int = 0
    tvdbId: int = 0
    finaleType: str | None = None

    @field_validator("airDate", mode="before")
    @classmethod
    def _empty_air_date_to_none(cls, value: Any) -> Any:
        """将未排期剧集可能传入的空字符串 ``""`` 归一化为 ``None``。"""
        if value == "":
            return None
        return value


class FileInfoDto(ArrWebhookBaseModel):
    """Sonarr 剧集文件信息（对应 ``WebhookEpisodeFile``）。"""

    id: int
    relativePath: str
    path: str
    quality: str
    qualityVersion: int
    # 无发布组名或手工导入时可能为 null/省略
    releaseGroup: str | None = None
    sceneName: str | None = None
    size: int
    dateAdded: datetime
    languages: list[Languages] = Field(default_factory=list)
    mediaInfo: MediaInfoDto | None = None
    sourcePath: str | None = None
    recycleBinPath: str | None = None


class SonarrRelease(ArrWebhookBaseModel):
    """Sonarr 种子/发布信息（合并 ``WebhookRelease`` 与 ``WebhookGrabbedRelease``）。"""

    quality: str | None = None
    qualityVersion: int | None = None
    releaseGroup: str | None = None
    releaseTitle: str | None = None
    indexer: str | None = None
    size: int | None = None
    customFormatScore: int | None = None
    customFormats: list[str] = Field(default_factory=list)
    languages: list[Languages] = Field(default_factory=list)
    # ReleaseType 枚举：unknown / singleEpisode / multiEpisode / seasonPack / multiSeasonPack
    releaseType: str | None = None
    indexerFlags: list[str] = Field(default_factory=list)


class RenamedEpisodeFiles(FileInfoDto):
    """重命名剧集文件记录（对应继承自 ``WebhookEpisodeFile`` 的 ``WebhookRenamedEpisodeFile``）。"""

    previousRelativePath: str
    previousPath: str


class DownloadInfo(ArrWebhookBaseModel):
    """下载客户端任务摘要信息（对应 ``WebhookDownloadClientItem``）。"""

    quality: str
    qualityVersion: int
    title: str | None = None
    indexer: str | None = None
    size: int


class SonarrWebhookGrabPayload(WebhookBase[Literal["Grab"]]):
    """Sonarr Grab 事件（对应 ``WebhookGrabPayload``）。"""

    eventType: Literal["Grab"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    release: SonarrRelease
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    customFormatInfo: CustomFormatInfoDto | None = None


class SonarrWebhookDownloadPayload(WebhookBase[Literal["Download"]]):
    """Sonarr Download / ImportComplete 事件（合并 ``WebhookImportPayload`` 与 ``WebhookImportCompletePayload``）。"""

    eventType: Literal["Download"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    # 单文件入库使用 episodeFile，整包完成入库（ImportComplete）使用 episodeFiles
    episodeFile: FileInfoDto | None = None
    episodeFiles: list[FileInfoDto] = Field(default_factory=list)
    release: SonarrRelease | None = None
    isUpgrade: bool | None = None
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    customFormatInfo: CustomFormatInfoDto | None = None
    deletedFiles: list[FileInfoDto] = Field(default_factory=list)
    fileCount: int | None = None
    sourcePath: str | None = None
    destinationPath: str | None = None


class SonarrWebhookEpisodeDeletePayload(WebhookBase[Literal["EpisodeFileDelete"]]):
    """Sonarr 剧集文件删除事件（对应 ``WebhookEpisodeDeletePayload``）。"""

    eventType: Literal["EpisodeFileDelete"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    episodeFile: FileInfoDto
    # DeleteMediaFileReason 枚举：missingFromDisk / manual / upgrade / noLinkedEpisodes / manualOverride
    deleteReason: str


class SonarrWebhookSeriesAddPayload(WebhookBase[Literal["SeriesAdd"]]):
    """Sonarr 剧集添加事件（对应 ``WebhookSeriesAddPayload``）。"""

    eventType: Literal["SeriesAdd"]
    series: SonarrSeries


class SonarrWebhookSeriesDeletePayload(WebhookBase[Literal["SeriesDelete"]]):
    """Sonarr 剧集删除事件（对应 ``WebhookSeriesDeletePayload``）。"""

    eventType: Literal["SeriesDelete"]
    series: SonarrSeries
    deletedFiles: bool


class SonarrWebhookRenamePayload(WebhookBase[Literal["Rename"]]):
    """Sonarr 重命名事件（对应 ``WebhookRenamePayload``）。"""

    eventType: Literal["Rename"]
    series: SonarrSeries
    renamedEpisodeFiles: list[RenamedEpisodeFiles] = Field(default_factory=list)


class SonarrWebhookHealthPayload(WebhookBase[Literal["Health", "HealthRestored"]]):
    """Sonarr 健康检查及恢复事件（对应 ``WebhookHealthPayload``）。"""

    eventType: Literal["Health", "HealthRestored"]
    # HealthCheckResult 枚举：ok / notice / warning / error
    level: str
    message: str
    type: str
    wikiUrl: str | None = None


class SonarrWebhookApplicationUpdatePayload(WebhookBase[Literal["ApplicationUpdate"]]):
    """Sonarr 应用更新事件（对应 ``WebhookApplicationUpdatePayload``）。"""

    eventType: Literal["ApplicationUpdate"]
    message: str
    previousVersion: str
    newVersion: str


class SonarrWebhookManualInteractionPayload(
    WebhookBase[Literal["ManualInteractionRequired"]]
):
    """Sonarr 需要人工干预事件（对应 ``WebhookManualInteractionPayload``）。"""

    eventType: Literal["ManualInteractionRequired"]
    series: SonarrSeries | None = None
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    downloadInfo: DownloadInfo
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    downloadStatus: str | None = None
    downloadStatusMessages: list[DownloadStatusMessage] = Field(default_factory=list)
    customFormatInfo: CustomFormatInfoDto | None = None
    release: SonarrRelease | None = None


class SonarrWebhookTestPayload(WebhookBase[Literal["Test"]]):
    """Sonarr Webhook 测试事件（上游复用 ``WebhookGrabPayload`` 发送测试样例）。"""

    eventType: Literal["Test"]
    series: SonarrSeries | None = None
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    release: SonarrRelease | None = None
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    customFormatInfo: CustomFormatInfoDto | None = None


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
