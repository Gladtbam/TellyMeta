from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping


class TmdbTvSeries(BaseModel):
    id: int
    name: str
    original_name: str
    overview: str
    first_air_date: str
    genre_ids: list[int | str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)

    @field_validator('genre_ids', mode='before')
    @classmethod
    def validate_genre_ids(cls, value):
        if isinstance(value, list):
            return [genre_mapping.get(genre, genre) for genre in value]
        elif isinstance(value, str):
            return [genre_mapping.get(value, value)]
        else:
            raise ValueError("无效的genre_ids格式，预期的列表或字符串")

    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if isinstance(value, list):
            results = []
            for genre in value:
                key = None
                if isinstance(genre, dict):
                    key = genre.get('id') or genre.get('name')
                else:
                    key = genre

                if key is not None:
                    results.append(genre_mapping.get(key, key))
            return results
        elif isinstance(value, str):
            return [genre_mapping.get(value, value)]
        else:
            raise ValueError("类型格式、预期列表或字符串无效")

class TmdbEpisode(BaseModel):
    id: int
    name: str
    overview: str
    media_type: str | None = None
    air_date: str | None = None
    episode_number: int
    episode_type: str
    runtime: int | None = None
    season_number: int
    show_id: int | None = None
    vote_average: float | None = None
    vote_count: int | None = None

class TmdbSeason(BaseModel):
    _id: str
    air_date: str
    name: str
    overview: str
    id: int
    season_number: int
    vote_average: float | None = None
    episodes: list[TmdbEpisode] = Field(default_factory=list)

    @field_validator('episodes', mode='before')
    @classmethod
    def validate_episodes(cls, value):
        if isinstance(value, list):
            return [TmdbEpisode.model_validate(episode) for episode in value]
        return []

class TmdbFindPayload(BaseModel):
    tv_results: list[TmdbTvSeries] = Field(default_factory=list)
    tv_episode_results: list[TmdbEpisode] = Field(default_factory=list)

    @field_validator('tv_results', mode='before')
    @classmethod
    def validate_tv_results(cls, value):
        if isinstance(value, list):
            return [TmdbTvSeries.model_validate(tv) for tv in value]
        elif isinstance(value, dict):
            return [TmdbTvSeries.model_validate(value)]
        else:
            raise ValueError("tv_results 格式无效，需要列表或字典")
    @field_validator('tv_episode_results', mode='before')
    @classmethod
    def validate_tv_episode_results(cls, value):
        if isinstance(value, list):
            return [TmdbEpisode.model_validate(episode) for episode in value]
        elif isinstance(value, dict):
            return [TmdbEpisode.model_validate(value)]
        else:
            raise ValueError("tv_episode_results 格式、预期列表或字典无效")

class TmdbMovie(BaseModel):
    id: int
    imdb_id: str
    origin_country: list[str] = Field(default_factory=list)
    original_language: str
    original_title: str
    overview: str
    popularity: float | None = None
    poster_path: str
    release_date: str
    runtime: int | None = None
    status: str
    title: str
    vote_average: float | None = None
    vote_count: int | None = None
