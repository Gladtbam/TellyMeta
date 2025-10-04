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
            raise ValueError("Invalid genre_ids format, expected list or string")
    
    @field_validator('genres', mode='before')
    @classmethod
    def validate_genres(cls, value):
        if isinstance(value, list):
            return [genre_mapping.get(genre.get('id') or genre.get('name') if isinstance(genre, dict) else genre) for genre in value]
        elif isinstance(value, str):
            return [genre_mapping.get(value, value)]
        else:
            raise ValueError("Invalid genres format, expected list or string")

class TmdbEpisode(BaseModel):
    id: int
    name: str
    overview: str
    air_date: str
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
