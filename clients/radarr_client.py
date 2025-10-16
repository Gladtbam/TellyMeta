import httpx

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.radarr import MovieResource

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
            dict | None: 返回电影信息的字典，如果查询失败则返回 None。
        """
        url = "/api/v3/movie/lookup/tmdb"
        params = {'tmdbId': tmdb_id}
        response = await self.get(url, params=params, response_model=MovieResource)
        return response if isinstance(response, MovieResource) else None

    async def get_movie_by_tmdb(self, tmdb_id: int) -> MovieResource | None:
        """根据 TMDB ID 获取 Radarr 中的电影信息。
        Args:
            tmdb_id (int): TMDB 电影 ID。
        Returns:
            dict | None: 返回电影信息的字典，如果查询失败则返回 None。
        """
        url = "/api/v3/movie"
        params = {'tmdbId': tmdb_id}
        response = await self.get(url, params=params, response_model=MovieResource)
        return response if isinstance(response, MovieResource) else None

    async def post_movie(self, movie_resource: MovieResource) -> MovieResource | None:
        """向 Radarr 添加电影。
        Args:
            movie_resource (MovieResource): 要添加的电影信息。
        Returns:
            dict | None: 返回添加后的电影信息的字典，如果添加失败则返回 None。
        """
        url = "/api/v3/movie"
        response = await self.post(url, json=movie_resource.model_dump(), response_model=MovieResource)
        return response if isinstance(response, MovieResource) else None
