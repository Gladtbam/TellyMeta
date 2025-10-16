import httpx

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.tmdb import TmdbFindPayload, TmdbTv

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

    async def find_info_by_external_id(
        self,
        external_source: str,
        external_id: str
    ) -> None | TmdbFindPayload | httpx.Response:
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

    async def get_tv_details(self, tmdb_id: int) -> None | TmdbTv | httpx.Response:
        """根据 TMDB ID 获取 TMDB 电视剧详情。
        Args:
            tmdb_id (int): TMDB 电视剧 ID。
        Returns:
            TmdbTv | None: TmdbTv 对象，如果查询失败则返回 None。
        """
        url = f"/tv/{tmdb_id}"
        params = {"language": "zh-CN"}
        return await self.get(url, params=params, response_model=TmdbTv)
