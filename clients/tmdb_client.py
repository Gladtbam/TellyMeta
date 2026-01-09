import asyncio

import httpx
from loguru import logger

from clients.base_client import AuthenticatedClient, RateLimiter
from models.tmdb import TmdbFindPayload, TmdbMovie, TmdbSeason, TmdbTvSeries


class TmdbClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        super().__init__(client)
        self.api_key = api_key
        self._limiter = RateLimiter(rate=30, per=1.0)

    async def _login(self) -> None:
        # TMDB 使用 Bearer Token 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json"
        }

    async def _request(self, *args, **kwargs):
        """
        重写 _request 方法以添加速率限制和 429 重试逻辑
        """
        await self._limiter.acquire()

        try:
            return await super()._request(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning(f"TMDB 速率限制已触发 (429)。正在等待重试... URL: {e.request.url}")

                retry_after = e.response.headers.get("Retry-After")
                try:
                    sleep_time = int(retry_after) + 1 if retry_after else 1
                except (ValueError, TypeError):
                    sleep_time = 1

                await asyncio.sleep(sleep_time)

                return await self._request(*args, **kwargs)

            raise

    async def find_info_by_external_id(
        self,
        external_source: str,
        external_id: str
    ) -> TmdbFindPayload | None:
        """根据外部 ID 获取 TMDB 相关信息。
        Args:
            external_source (str): 外部来源，如 "imdb_id" 或 "tvdb_id"。
            external_id (str): 外部 ID 值。
        Returns:
            TmdbFindPayload | None: TmdbFindPayload 对象，如果查询失败则返回 None。
        """
        url = f"/find/{external_id}"
        params = {
            "external_source": external_source,
            "language": "zh-CN"
        }

        return await self.get(url, params=params, response_model=TmdbFindPayload)

    async def get_tv_series_details(self, tmdb_id: int) -> TmdbTvSeries | None:
        """根据 TMDB ID 获取 TMDB 电视剧详情。
        Args:
            tmdb_id (int): TMDB 电视剧 ID。
        Returns:
            TmdbTvSeries | None: TmdbTvSeries 对象，如果查询失败则返回 None。
        """
        url = f"/tv/{tmdb_id}"
        params = {"language": "zh-CN"}
        return await self.get(url, params=params, response_model=TmdbTvSeries)

    async def get_tv_seasons_details(self, tmdb_id: int, season_number: int) -> TmdbSeason | None:
        """根据 TMDB ID 和季节号获取 TMDB 电视剧季节详情。
        Args:
            tmdb_id (int): TMDB 电视剧 ID。
            season_number (int): 季节号。
        Returns:
            TmdbSeason | None: 电视剧季节详情的字典，如果查询失败则返回 None。
        """
        url = f"/tv/{tmdb_id}/season/{season_number}"
        params = {"language": "zh-CN"}
        return await self.get(url, params=params, response_model=TmdbSeason)

    async def get_movie_details(self, tmdb_id: int) -> TmdbMovie | None:
        """根据 TMDB ID 获取 TMDB 电影详情。
        Args:
            tmdb_id (int): TMDB 电影 ID。
        Returns:
            TmdbMovie | None: TmdbMovie 对象，如果查询失败则返回 None。
        """
        url = f"/movie/{tmdb_id}"
        params = {"language": "zh-CN"}
        return await self.get(url, params=params, response_model=TmdbMovie)
