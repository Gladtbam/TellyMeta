# from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping
from models.radarr import (CustomFormatResource, Languages, MediaCover,
                           MediaInfoDto, MediaInfoResource, QualityModel,
                           WebhookBase, customFormatInfoDto)


class AlternativeTitleResource(BaseModel):
    """Sonarr 剧集别名模型"""
    title: str | None = None
    seasonNumber: int | None = None
    sceneSeasonNumber: int | None = None
    sceneOrigin: str | None = None
    comment: str | None = None

class SeasonStatisticsResource(BaseModel):
    """Sonarr 季统计模型"""
    nextAiring: str | None = None
    previousAiring: str | None = None
    episodeFileCount: int
    episodeCount: int
    totalEpisodeCount: int
    sizeOnDisk: int
    releaseGroups: list[str] = Field(default_factory=list)
    percentOfEpisodes: float

class SeasonResource(BaseModel):
    """Sonarr 季模型"""
    seasonNumber: int
    monitored: bool
    statistics: SeasonStatisticsResource | None = None
    images: list[MediaCover] = Field(default_factory=list)

class AddSeriesOptions(BaseModel):
    """Sonarr 添加剧集选项模型"""
    ignoreEpisodesWithFiles: bool
    ignoreEpisodesWithoutFiles: bool
    monitor: str
    searchForMissingEpisodes: bool
    searchForCutoffUnmetEpisodes: bool

class Ratings(BaseModel):
    """Sonarr 剧集评分模型"""
    votes: int
    value: float

class SeriesStatisticsResource(BaseModel):
    """Sonarr 剧集统计模型"""
    seasonCount: int
    episodeFileCount: int
    episodeCount: int
    totalEpisodeCount: int
    sizeOnDisk: int
    releaseGroups: list[str] = Field(default_factory=list)
    percentOfEpisodes: float

class SeriesResource(BaseModel):
    """Sonarr 剧集模型"""
    id: int | None = None
    title: str | None = None
    alternateTitles: list[AlternativeTitleResource] = Field(default_factory=list)
    sortTitle: str | None = None
    status: str
    ended: bool
    profileName: str | None = None
    overview: str | None = None
    nextAiring: str | None = None
    previousAiring: str | None = None
    network: str | None = None
    airTime: str | None = None
    images: list[MediaCover] = Field(default_factory=list)
    originalLanguage: Languages | None = None
    remotePoster: str | None = None
    seasons: list[SeasonResource] = Field(default_factory=list)
    year: int
    path: str | None = None
    qualityProfileId: int
    seasonFolder: bool
    monitored: bool
    monitorNewItems: str | None = None
    useSceneNumbering: bool
    runtime: int
    tvdbId: int
    tvRageId: int
    tvMazeId: int
    tmdbId: int
    firstAired: str | None = None
    lastAired: str | None = None
    seriesType: str | None = None
    cleanTitle: str | None = None
    imdbId: str | None = None
    titleSlug: str | None = None
    rootFolderPath: str | None = None
    folder: str | None = None
    certification: str | None = None
    genres: list[str] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)
    added: str | None = None
    addOptions: AddSeriesOptions | None = None
    ratings: Ratings | None = None
    statistics: SeriesStatisticsResource | None = None
    episodesChanged: bool | None = None
    # languageProfileId: int | None = None # deprecated

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]

class EpisodeFileResource(BaseModel):
    """Sonarr 剧集文件模型"""
    id: int
    seriesId: int
    seasonNumber: int
    relativePath: str | None = None
    path: str | None = None
    size: int
    dateAdded: str
    sceneName: str | None = None
    releaseGroup: str | None = None
    languages: list[Languages] = Field(default_factory=list)
    quality: QualityModel | None = None
    customFormats: list[CustomFormatResource] = Field(default_factory=list)
    customFormatScore: int
    indexerFlags: int | None = None
    releaseType: str | None = None
    mediaInfo: MediaInfoResource | None = None
    qualityCutoffNotMet: bool

class EpisodeResource(BaseModel):
    """Sonarr 剧集详细信息模型"""
    id: int
    seriesId: int
    tvdbId: int
    episodeFileId: int
    seasonNumber: int
    episodeNumber: int
    title: str | None = None
    airDate: str | None = None
    airDateUtc: str | None = None
    lastSearchTime: str | None = None
    runtime: int
    finaleType: str | None = None
    overview: str | None = None
    episodeFile: EpisodeFileResource | None = None
    hasFile: bool
    monitored: bool
    absoluteEpisodeNumber: int | None = None
    sceneAbsoluteEpisodeNumber: int | None = None
    sceneEpisodeNumber: int | None = None
    sceneSeasonNumber: int | None = None
    unverifiedSceneNumbering: bool
    endtime: str | None = None
    grabDate: str | None = None
    series: SeriesResource | None = None
    images: list[MediaCover] = Field(default_factory=list)

#====================================================
#      SonarrWebhook
#====================================================
class SonarrSeries(BaseModel):
    """Sonarr series 模型"""
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

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]

class SonarrEpisode(BaseModel):
    """Sonarr 剧集模型"""
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
    """Sonarr 文件模型"""
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
    sourcePath: str| None = None

class SonarrRelease(BaseModel):
    """release"""
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
    previousRelativePath: str
    previousPath: str

class DownloadInfo(BaseModel):
    quality: str
    qualityVersion: int
    title: str | None = None
    indexer: str | None = None
    size: int

class SonarrWebhookGrabPayload(WebhookBase[Literal["Grab"]]):
    eventType: Literal["Grab"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    release: SonarrRelease
    downloadClient: str
    downloadClientType: str
    downloadId: str
    customFormatInfo: customFormatInfoDto

class SonarrWebhookDownloadPayload(WebhookBase[Literal["Download"]]):
    """
        SonarrWebhookImportPayload
        SonarrWebhookImportCompletePayload
    """
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
    eventType: Literal["EpisodeFileDelete"]
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    episodeFile: FileInfoDto
    deleteReason: str

class SonarrWebhookSeriesAddPayload(WebhookBase[Literal["SeriesAdd"]]):
    eventType: Literal["SeriesAdd"]
    series: SonarrSeries

class SonarrWebhookSeriesDeletePayload(WebhookBase[Literal["SeriesDelete"]]):
    eventType: Literal["SeriesDelete"]
    series: SonarrSeries
    deletedFiles: bool

class SonarrWebhookRenamePayload(WebhookBase[Literal["Rename"]]):
    eventType: Literal["Rename"]
    series: SonarrSeries
    renamedEpisodeFiles: list[RenamedEpisodeFiles] = Field(default_factory=list)

class SonarrWebhookHealthPayload(WebhookBase[Literal["Health", "HealthRestored"]]):
    eventType: Literal["Health", "HealthRestored"]
    level: str
    message: str
    type: str
    wikiUrl: str | None = None

class SonarrWebhookApplicationUpdatePayload(WebhookBase[Literal["ApplicationUpdate"]]):
    eventType: Literal["ApplicationUpdate"]
    message: str
    previousVersion: str
    newVersion: str

class SonarrWebhookManualInteractionPayload(WebhookBase[Literal["ManualInteractionRequired"]]):
    eventType: Literal["ManualInteractionRequired"]
    series: SonarrSeries | None = None # 未知剧集为 None
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    downloadInfo: DownloadInfo
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str
    downloadStatus: str
    # downloadStatusMessages: list[dict] = Field(default_factory=list)
    customFormatInfo: customFormatInfoDto
    # release: SonarrRelease

class SonarrWebhookTestPayload(WebhookBase[Literal["Test"]]):
    eventType: Literal["Test"]

SonarrPayload = Annotated[
    SonarrWebhookGrabPayload |
    SonarrWebhookDownloadPayload |
    SonarrWebhookEpisodeDeletePayload |
    SonarrWebhookSeriesAddPayload |
    SonarrWebhookSeriesDeletePayload |
    SonarrWebhookRenamePayload |
    SonarrWebhookHealthPayload |
    SonarrWebhookApplicationUpdatePayload |
    SonarrWebhookManualInteractionPayload |
    SonarrWebhookTestPayload,
    Field(discriminator="eventType")
]
