from collections.abc import AsyncGenerator

import httpx
from loguru import logger
from pydantic import TypeAdapter, ValidationError

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.radarr import (AddMovieOptions, MovieResource,
                           QualityProfileResource, RootFolderResource)

setting = get_settings()

class RadarrClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        super().__init__(client)
        self.api_key = api_key

    async def _login(self) -> None:
        # Radarr 使用 API Key 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "X-Api-Key": self.api_key,
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    async def lookup_by_tmdb(self, tmdb_id: int) -> MovieResource | None:
        """根据 TMDB ID 查找 The Movie Database 获取的电影信息。
        Args:
            tmdb_id (int): TMDB 电影 ID。
        Returns:
            MovieResource | None: 返回电影信息，如果查询失败则返回 None。
        """
        url = "/api/v3/movie/lookup/tmdb"
        params = {'tmdbId': tmdb_id}
        response = await self.get(url, params=params, response_model=MovieResource)
        return response if isinstance(response, MovieResource) else None

    async def lookup(self, term: str) -> AsyncGenerator[MovieResource, None]:
        """根据电影名称查找 Radarr 中的电影信息。
        Args:
            term (str): 电影名称。
        Returns:
            AsyncGenerator[MovieResource, None]: 返回电影信息的生成器。
        """
        url = "/api/v3/movie/lookup"
        params = {'term': term}
        response = await self.get(url, params=params)
        if response is None:
            return

        try:
            movies = TypeAdapter(list[MovieResource]).validate_python(response.json())
            for movie in movies:
                yield movie
        except ValidationError as e:
            logger.error("无法解析电影查找响应：{}", repr(e.errors()))
            return

    async def get_movie_by_tmdb(self, tmdb_id: int) -> MovieResource | None:
        """根据 TMDB ID 获取 Radarr 中的电影信息。
        Args:
            tmdb_id (int): TMDB 电影 ID。
        Returns:
            MovieResource | None: 返回电影信息，如果查询失败则返回 None。
        """
        url = "/api/v3/movie"
        params = {'tmdbId': tmdb_id}
        response = await self.get(url, params=params)
        if response is None:
            return None

        try:
            movies = TypeAdapter(list[MovieResource]).validate_python(response.json())
            return movies[0] if movies else None
        except ValidationError as e:
            logger.error("无法解析电影查找响应：{}", repr(e.errors()))
            return None

    async def post_movie(self, movie_resource: MovieResource) -> MovieResource | None:
        """向 Radarr 添加电影。
        Args:
            movie_resource (MovieResource): 要添加的电影信息。
        Returns:
            MovieResource | None: 返回添加后的电影信息，如果添加失败则返回 None。
        """
        url = "/api/v3/movie"
        movie_resource.monitored = True
        movie_resource.minimumAvailability = "released"
        movie_resource.addOptions = AddMovieOptions(
            ignoreEpisodesWithFiles = False,
            ignoreEpisodesWithoutFiles = False,
            monitor = "movieOnly",
            searchForMovie = True,
            addMethod = "manual"
        )
        response = await self.post(url,
            json=movie_resource.model_dump(exclude_unset=True),
            response_model=MovieResource)
        return response if isinstance(response, MovieResource) else None

    async def get_root_folders(self) -> list[RootFolderResource] | None:
        """获取 Radarr 的根文件夹列表。
        Returns:
            list[RootFolderResource] | None: 返回根文件夹路径的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/rootfolder"
        response = await self.get(url)
        if response is None:
            return None

        try:
            return TypeAdapter(list[RootFolderResource]).validate_python(response.json())
        except ValidationError as e:
            logger.error("无法解析根文件夹响应：{}", repr(e.errors()))
            return None

    async def get_quality_profiles(self) -> list[QualityProfileResource] | None:
        """获取 Radarr 的质量配置文件列表。
        Returns:
            list[QualityProfileResource] | None: 返回质量配置文件的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/qualityprofile"
        response = await self.get(url)
        if response is None:
            return None

        try:
            return TypeAdapter(list[QualityProfileResource]).validate_python(response.json())
        except ValidationError as e:
            logger.error("无法解析质量配置文件响应：{}", repr(e.errors()))
            return None
