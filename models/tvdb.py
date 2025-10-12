# from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping

class TvdbData(BaseModel):
    token: str | None = None
    name: str | None = None
    overview: str | None = None
    language: str | None = None

class TvdbSeasonType(BaseModel):
    id: int
    name: str
    type: str
    alternateName: str | None = None

class TvdbSeasonsData(BaseModel):
    id: int
    seriesId: int
    type: list[TvdbSeasonType] = Field(default_factory=list)
    number: int
    nameTranslations: list[str] = Field(default_factory=list)
    overviewTranslations: list[str] = Field(default_factory=list)
    image: str | None = None
    imageType: int | None = None
    lastUpdated: str | None = None
    year: str | None = None
    episodes: list['TvdbEpisodesData'] = Field(default_factory=list)

class TvdbTranslations(BaseModel):
    name: str | None = None
    overview: str | None = None
    language: str | None = None

class TvdbTranslationsData(BaseModel):
    nameTranslations: list[TvdbTranslations] = Field(default_factory=list)
    overviewTranslations: list[TvdbTranslations] = Field(default_factory=list)

class TvdbEpisodesData(BaseModel):
    id: int
    seriesId: int
    name: str
    aired: str
    runtime: int
    nameTranslations: list[str] = Field(default_factory=list)
    overview: str | None = None
    overviewTranslations: list[str] = Field(default_factory=list)
    image: str | None = None
    imageType: int | None = None
    isMovie: bool
    seasons: list[TvdbSeasonsData] = Field(default_factory=list)
    number: int
    absoluteNumber: int
    seasonNumber: int
    lastUpdated: str
    year: str
    translations: list[TvdbTranslationsData] = Field(default_factory=list)

class TvdbSeriesData(BaseModel):
    id: int
    name: str
    overview: str
    image: str
    nameTranslations: list[str] = Field(default_factory=list)
    overviewTranslations: list[str] = Field(default_factory=list)
    aliases: list[TvdbTranslations] = Field(default_factory=list)
    firstAired: str | None = None
    lastAired: str | None = None
    nextAired: str | None = None
    score: int | None = None
    originalCountry: str | None = None
    originalLanguage: str | None = None
    lastUpdated: str | None = None
    averageRuntime: int | None = None
    episodes: list[TvdbEpisodesData] = Field(default_factory=list)
    year: str | None = None
    genres:list = Field(default_factory=list)
    seasons: list[TvdbSeasonsData] = Field(default_factory=list)
    translations: TvdbTranslationsData | None = None

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if isinstance(value, list):
            return [genre_mapping.get(genre.get('id') or genre.get('name') if isinstance(genre, dict) else genre) for genre in value]
        elif isinstance(value, str):
            return [genre_mapping.get(value, value)]
        else:
            raise ValueError("Invalid genres format, expected list or string")

class TvdbPayload(BaseModel):
    status: str
    data: TvdbData | TvdbEpisodesData | TvdbSeasonsData | TvdbSeriesData | None = None
    message: str | None = None

# 为存在循环引用的模型重建
TvdbSeasonsData.model_rebuild()
TvdbEpisodesData.model_rebuild()
TvdbSeriesData.model_rebuild()