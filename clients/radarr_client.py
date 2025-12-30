from collections.abc import AsyncGenerator

import httpx
from pydantic import TypeAdapter

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.radarr import (AddMovieOptions, MovieResource,
                           QualityProfileResource, RootFolderResource)

setting = get_settings()

class RadarrClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str, path_mappings: dict[str, str] | None = None):
        super().__init__(client)
        self.api_key = api_key
        self.path_mappings = path_mappings or {}

    async def _login(self) -> None:
        # Radarr 使用 API Key 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "X-Api-Key": self.api_key,
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    def to_local_path(self, remote_path: str | None) -> str | None:
        """路径映射，将远程路径转换为本地路径"""
        if not remote_path:
            return remote_path
        
        for remote, local in self.path_mappings.items():
            if remote_path.startswith(remote):
                return remote_path.replace(remote, local, 1)
        return remote_path

    async def lookup_by_tmdb(self, tmdb_id: int) -> MovieResource | None:
        """根据 TMDB ID 查找 The Movie Database 获取的电影信息。
        Args:
            tmdb_id (int): TMDB 电影 ID。
        Returns:
            MovieResource | None: 返回电影信息，如果查询失败则返回 None。
        """
        url = "/api/v3/movie/lookup/tmdb"
        params = {'tmdbId': tmdb_id}
        movie = await self.get(url, params=params, response_model=MovieResource)
        if movie:
            movie.path = self.to_local_path(movie.path)
        return movie

    async def lookup(self, term: str) -> AsyncGenerator[MovieResource, None]:
        """根据电影名称查找 Radarr 中的电影信息。
        Args:
            term (str): 电影名称。
        Returns:
            AsyncGenerator[MovieResource, None]: 返回电影信息的生成器。
        """
        url = "/api/v3/movie/lookup"
        params = {'term': term}
        response = await self.get(url, params=params,
            parser=lambda data: TypeAdapter(list[MovieResource]).validate_python(data))
        if response is None:
            return

        for movie in response:
            movie.path = self.to_local_path(movie.path)
            yield movie

    async def get_movie_by_tmdb(self, tmdb_id: int) -> MovieResource | None:
        """根据 TMDB ID 获取 Radarr 中的电影信息。
        Args:
            tmdb_id (int): TMDB 电影 ID。
        Returns:
            MovieResource | None: 返回电影信息，如果查询失败则返回 None。
        """
        url = "/api/v3/movie"
        params = {'tmdbId': tmdb_id}
        response = await self.get(url, params=params,
            parser=lambda data: TypeAdapter(list[MovieResource]).validate_python(data))

        if response and response[0]:
            movie = response[0]
            movie.path = self.to_local_path(movie.path)
            if movie.movieFile:
                movie.movieFile.path = self.to_local_path(movie.movieFile.path)
            return movie
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
        return await self.post(url,
            json=movie_resource.model_dump(exclude_unset=True),
            response_model=MovieResource)

    async def get_root_folders(self) -> list[RootFolderResource] | None:
        """获取 Radarr 的根文件夹列表。
        Returns:
            list[RootFolderResource] | None: 返回根文件夹路径的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/rootfolder"
        return await self.get(url,
            parser=lambda data: TypeAdapter(list[RootFolderResource]).validate_python(data))

    async def get_quality_profiles(self) -> list[QualityProfileResource] | None:
        """获取 Radarr 的质量配置文件列表。
        Returns:
            list[QualityProfileResource] | None: 返回质量配置文件的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/qualityprofile"
        return await self.get(url,
            parser=lambda data: TypeAdapter(list[QualityProfileResource]).validate_python(data))
