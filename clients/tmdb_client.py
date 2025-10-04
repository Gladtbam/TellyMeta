import logging

import httpx

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.tmdb import TmdbFindPayload, TmdbTv

logger = logging.getLogger(__name__)
setting = get_settings()

class TmdbService(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        super().__init__(client)
        self.api_key = api_key

    async def _login(self) -> None:
        # TMDB 使用 Bearer Token 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json"
        }

    async def get_info(
        self,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None
    ) -> TmdbTv | TmdbFindPayload | None:
        """根据 TMDB ID、IMDB ID 或 TVDB ID 获取 TMDB 电视剧信息。
        优先使用 TMDB ID 进行查询，如果没有提供，则依次尝试 IMDB ID 和 TVDB ID。
        Args:
            tmdb_id (int | None): TMDB 电视剧 ID。
            imdb_id (str | None): IMDB 电视剧 ID。
            tvdb_id (int | None): TVDB 电视剧 ID。
        Returns:
            TmdbTv | TmdbFindPayload | None: 返回 TmdbTv 对象（如果使用 TMDB ID 查询）或 TmdbFindPayload 对象（如果使用 IMDB ID 或 TVDB ID 查询），如果查询失败则返回 None。
        """
        if tmdb_id:
            url = f"/tv/{tmdb_id}?language=zh-CN"
        elif imdb_id:
            url = f"/find/{imdb_id}?external_source=imdb_id&language=zh-CN"
        elif tvdb_id:
            url = f"/find/{tvdb_id}?external_source=tvdb_id&language=zh-CN"
        else:
            logging.error("没有提供有效的 ID（TMDB ID、IMDB ID 或 TVDB ID）进行查询")
            return None

        response = await self.get(url)
        data = response.json()
        if tmdb_id:
            return TmdbTv.model_validate(data)
        if imdb_id or tvdb_id:
            return TmdbFindPayload.model_validate(data)
        return None
