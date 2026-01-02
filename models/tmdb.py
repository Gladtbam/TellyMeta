from pydantic import BaseModel, Field, field_validator

from core.config import genre_mapping


class TmdbTv(BaseModel):
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
            return [genre_mapping.get(genre.get('id') or genre.get('name') if isinstance(genre, dict) else genre) for genre in value]
        elif isinstance(value, str):
            return [genre_mapping.get(value, value)]
        else:
            raise ValueError("类型格式、预期列表或字符串无效")

class TmdbEpisode(BaseModel):
    id: int
    name: str
    overview: str
    air_date: str | None = None
    season_number: int
    episode_number: int

class TmdbFindPayload(BaseModel):
    tv_results: list[TmdbTv] = Field(default_factory=list)
    tv_episode_results: list[TmdbEpisode] = Field(default_factory=list)
    
    @field_validator('tv_results', mode='before')
    @classmethod
    def validate_tv_results(cls, value):
        if isinstance(value, list):
            return [TmdbTv.model_validate(tv) for tv in value]
        elif isinstance(value, dict):
            return [TmdbTv.model_validate(value)]
        else:
            raise ValueError("Invalid tv_results format, expected list or dict")
    @field_validator('tv_episode_results', mode='before')
    @classmethod
    def validate_tv_episode_results(cls, value):
        if isinstance(value, list):
            return [TmdbEpisode.model_validate(episode) for episode in value]
        elif isinstance(value, dict):
            return [TmdbEpisode.model_validate(value)]
        else:
            raise ValueError("Invalid tv_episode_results format, expected list or dict")

class TmdbMovie(BaseModel):
    id: int
    imdb_id: str
    origin_country: list[str] = Field(default_factory=list)
    original_language: str
    original_title: str
    overview: str
    popularity: float = 0
    poster_path: str
    release_date: str
    runtime: int = 0
    status: str
    title: str
    vote_average: float = 0
    vote_count: int = 0
