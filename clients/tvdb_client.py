import httpx
from loguru import logger

from clients.base_client import AuthenticatedClient
from models.tvdb import TvdbData, TvdbPayload


class TvdbClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        super().__init__(client)
        self.api_key = api_key
        self._token: str | None = None

    async def _login(self):
        payload = {'apikey': self.api_key}
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }
        if self._client is None:
            logger.warning("HTTP 客户端未初始化。首先调用 login()。")
        response = await self.post("login", json=payload, headers=headers, response_model=TvdbPayload)
        if isinstance(response, TvdbPayload) and isinstance(response.data, TvdbData):
            self._token = response.data.model_dump().get('token')
        else:
            self._token = None
            logger.error("登录 TVDB 失败。检查您的 API 密钥。")

    async def _apply_auth(self):
        if self._token:
            return {'Authorization': f'Bearer {self._token}'}
        else:
            return {}

    async def episodes_translations(self, episode_id: int, language: str = 'zho') -> TvdbPayload | None:
        """获取指定剧集的翻译信息

        Args:
            episode_id (int): 剧集ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据"""
        response = await self.get(
            f"/episodes/{episode_id}/translations/{language}",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        return response if isinstance(response, TvdbPayload) else None

    async def episodes_extended(self, episode_id: int) -> TvdbPayload | None:
        """获取指定剧集的扩展信息

        Args:
            episode_id (int): 剧集ID
        Returns:
            TvdbPayload: 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/episodes/{episode_id}/extended",
            params={'meta': 'translations'},
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        return response if isinstance(response, TvdbPayload) else None

    async def seasons_translations(self, season_id: int, language: str = 'zho') -> TvdbPayload | None:
        """获取指定季的翻译信息

        Args:
            season_id (int): 季ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据
        """
        response = await self.get(
            f"/seasons/{season_id}/translations/{language}",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        return response if isinstance(response, TvdbPayload) else None

    async def seasons_extended(self, season_id: int) -> TvdbPayload | None:
        """获取指定季的扩展信息

        Args:
            season_id (int): 季ID
        Returns:
            TvdbPayload: 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/seasons/{season_id}/extended",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        return response if isinstance(response, TvdbPayload) else None

    async def series_extended(self, series_id: int, meta: str = 'translations') -> TvdbPayload | None:
        """获取指定剧集的扩展信息

        Args:
            series_id (int): 剧集ID
            meta (str): 扩展信息类型，默认为 'translations'，可选 'episodes'
        Returns:
            TvdbPayload: 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/series/{series_id}/extended",
            params={'meta': f'{meta}'},
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        return response if isinstance(response, TvdbPayload) else None

    async def series_translations(self, series_id: int, language: str = 'zho') -> TvdbPayload | None:
        """获取指定剧集的翻译信息

        Args:
            series_id (int): 剧集ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据
        """
        response = await self.get(
            f"/series/{series_id}/translations/{language}",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        return response if isinstance(response, TvdbPayload) else None
