# from __future__ import annotations

from datetime import time
from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping

class Languages(BaseModel):
    """Radarr 语言模型"""
    id: int
    name: str | None = None

class AlternativeTitleResource(BaseModel):
    """Radarr 电影别名模型"""
    id: int
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
    source: str
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
    runTime: int | time | None = None
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
    id: int
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
    runtime: int
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
