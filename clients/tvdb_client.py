import asyncio

import httpx
from loguru import logger
from pydantic import ValidationError

from clients.base_client import AuthenticatedClient, RateLimiter
from models.tvdb import TvdbData, TvdbEpisodesData, TvdbPayload, TvdbSeriesData


class TvdbClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        super().__init__(client)
        self.api_key = api_key
        self._token: str | None = None
        self._limiter = RateLimiter(rate=20, per=1.0)

    async def _login(self):
        payload = {'apikey': self.api_key}
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }
        if self._client is None:
            logger.warning("Tvdb 客户端未初始化。请先调用 login()。")
        response = await self._client.post("login", json=payload, headers=headers)
        response.raise_for_status()
        try:
            response_model = TvdbPayload.model_validate(response.json())
            if isinstance(response_model.data, TvdbData):
                self._token = response_model.data.token
                logger.info("成功登录 TVDB。")
        except ValidationError:
            self._token = None
            logger.error("登录 TVDB 失败。检查您的 API 密钥。")

    async def _request(self, *args, **kwargs):
        await self._limiter.acquire()

        try:
            return await super()._request(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning(f"TVDB 速率限制触发 (429)。URL: {e.request.url}")
                retry_after = e.response.headers.get("Retry-After")
                try:
                    sleep_time = int(retry_after) + 1 if retry_after else 1
                except (ValueError, TypeError):
                    sleep_time = 1

                await asyncio.sleep(sleep_time)
                return await self._request(*args, **kwargs)
            raise

    async def _apply_auth(self):
        if self._token:
            return {'Authorization': f'Bearer {self._token}'}
        return {}

    async def episodes_translations(self, episode_id: int, language: str = 'zho') -> TvdbPayload | None:
        """获取指定剧集的翻译信息

        Args:
            episode_id (int): 剧集ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据"""
        return await self.get(
            f"/episodes/{episode_id}/translations/{language}",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )

    async def episodes_extended(self, episode_id: int) -> TvdbEpisodesData | None:
        """获取指定剧集的扩展信息

        Args:
            episode_id (int): 剧集ID
        Returns:
            TvdbPayload (TvdbEpisodesData): 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/episodes/{episode_id}/extended",
            params={'meta': 'translations'},
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        if response is not None and isinstance(response.data, TvdbEpisodesData):
            return response.data
        return None

    async def seasons_translations(self, season_id: int, language: str = 'zho') -> TvdbPayload | None:
        """获取指定季的翻译信息

        Args:
            season_id (int): 季ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据
        """
        return await self.get(
            f"/seasons/{season_id}/translations/{language}",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )

    async def seasons_extended(self, season_id: int) -> TvdbPayload | None:
        """获取指定季的扩展信息

        Args:
            season_id (int): 季ID
        Returns:
            TvdbPayload: 包含扩展信息的响应数据
        """
        return await self.get(
            f"/seasons/{season_id}/extended",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )

    async def series_extended(self, series_id: int, meta: str = 'translations') -> TvdbSeriesData | None:
        """获取指定剧集的扩展信息

        Args:
            series_id (int): 剧集ID
            meta (str): 扩展信息类型，默认为 'translations'，可选 'episodes'
        Returns:
            TvdbPayload (TvdbSeriesData): 包含扩展信息的响应数据
        """
        response = await self.get(
            f"/series/{series_id}/extended",
            params={'meta': f'{meta}'},
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
        if response is not None and isinstance(response.data, TvdbSeriesData):
            return response.data
        return None

    async def series_translations(self, series_id: int, language: str = 'zho') -> TvdbPayload | None:
        """获取指定剧集的翻译信息

        Args:
            series_id (int): 剧集ID
            language (str): 语言代码，默认为 'zho'
        Returns:
            TvdbPayload: 包含翻译信息的响应数据
        """
        return await self.get(
            f"/series/{series_id}/translations/{language}",
            headers={'Accept': 'application/json'},
            response_model=TvdbPayload
        )
