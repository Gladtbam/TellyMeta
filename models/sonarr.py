# from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping
from models.radarr import (CustomFormatResource, Languages, MediaCover,
                           MediaInfoResource, QualityModel)


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

class SonarrSeries(BaseModel):
    """Sonarr 系列模型"""
    title: str
    path: str
    tvdbId: int
    tmdbId: int
    imdbId: str | None = None # Test WebHook
    year: int
    genres: list[str] = Field(default_factory=list)

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if not isinstance(value, list):
            return value
        return [genre_mapping.get(genre, genre) for genre in value]

class SonarrEpisode(BaseModel):
    """Sonarr 剧集模型"""
    seasonNumber: int
    episodeNumber: int
    tvdbId: int

class SonarrEpisodeFile(BaseModel):
    """Sonarr 剧集文件模型"""
    path: str

class SonarrPayload(BaseModel):
    """Sonarr Webhook 接收事件"""
    eventType: str
    downloadId: str | None = None
    series: SonarrSeries
    episodes: list[SonarrEpisode] = Field(default_factory=list)
    episodeFile: SonarrEpisodeFile | None = None
    instanceName: str | None = None
    applicationUrl: str | None = None
