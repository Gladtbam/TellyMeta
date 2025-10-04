import logging

import httpx

from clients.base_client import AuthenticatedClient
from models.tvdb import TvdbPayload

logger = logging.getLogger(__name__)

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
            raise RuntimeError("HTTP client is not initialized. Call login() first.")
        response = await self._client.post("login", json=payload, headers=headers)
        response.raise_for_status()
        self._token = TvdbPayload.model_validate(response.json()).data.token if response.status_code == 200 else None # type: ignore

    async def _apply_auth(self):
        if self._token:
            return {'Authorization': f'Bearer {self._token}'}
        else:
            return {}

    async def episodes_translations(self, episode_id: int, language: str = 'zho'):
        """获取指定剧集的翻译信息

        Args:
            episode_id (int): 剧集ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据"""
        response = await self.get(
            f"/episodes/{episode_id}/translations/{language}",
            headers={'Accept': 'application/json'}
        )
        return TvdbPayload.model_validate(response.json())

    async def episodes_extended(self, episode_id: int):
        """获取指定剧集的扩展信息

        Args:
            episode_id (int): 剧集ID
        Returns:
            TvdbPayload: 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/episodes/{episode_id}/extended",
            params={'meta': 'translations'},
            headers={'Accept': 'application/json'}
        )
        return TvdbPayload.model_validate(response.json())

    async def seasons_translations(self, season_id: int, language: str = 'zho'):
        """获取指定季的翻译信息

        Args:
            season_id (int): 季ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据
        """
        response = await self.get(
            f"/seasons/{season_id}/translations/{language}",
            headers={'Accept': 'application/json'}
        )
        return TvdbPayload.model_validate(response.json())

    async def seasons_extended(self, season_id: int):
        """获取指定季的扩展信息

        Args:
            season_id (int): 季ID
        Returns:
            TvdbPayload: 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/seasons/{season_id}/extended",
            headers={'Accept': 'application/json'}
        )
        return TvdbPayload.model_validate(response.json())

    async def series_extended(self, series_id: int, meta: str = 'translations'):
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
            headers={'Accept': 'application/json'}
        )
        return TvdbPayload.model_validate(response.json())

    async def series_translations(self, series_id: int, language: str = 'zho'):
        """获取指定剧集的翻译信息

        Args:
            series_id (int): 剧集ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据
        """
        response = await self.get(
            f"/series/{series_id}/translations/{language}",
            headers={'Accept': 'application/json'}
        )
        return TvdbPayload.model_validate(response.json())
