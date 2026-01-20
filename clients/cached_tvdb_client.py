import httpx
from loguru import logger

from clients.tvdb_client import TvdbClient
from models.tvdb import TvdbEpisodesData, TvdbPayload, TvdbSeriesData
from services.cache_service import CacheService


class CachedTvdbClient(TvdbClient):
    """带缓存的 TVDB 客户端"""

    def __init__(self, client: httpx.AsyncClient, api_key: str, cache_ttl: int = 86400 * 7):
        super().__init__(client, api_key)
        self.cache_ttl = cache_ttl  # 默认缓存 7 天

    async def episodes_translations(self, episode_id: int, language: str = 'zho') -> TvdbPayload | None:
        key = f"tvdb:episode:{episode_id}:translations:{language}"

        cached = await CacheService.get(key, TvdbPayload)
        if isinstance(cached, TvdbPayload):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().episodes_translations(episode_id, language)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def episodes_extended(self, episode_id: int) -> TvdbEpisodesData | None:
        key = f"tvdb:episode:{episode_id}:extended"

        cached = await CacheService.get(key, TvdbEpisodesData)
        if isinstance(cached, TvdbEpisodesData):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().episodes_extended(episode_id)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def seasons_translations(self, season_id: int, language: str = 'zho') -> TvdbPayload | None:
        key = f"tvdb:season:{season_id}:translations:{language}"

        cached = await CacheService.get(key, TvdbPayload)
        if isinstance(cached, TvdbPayload):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().seasons_translations(season_id, language)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def seasons_extended(self, season_id: int) -> TvdbPayload | None:
        key = f"tvdb:season:{season_id}:extended"

        cached = await CacheService.get(key, TvdbPayload)
        if isinstance(cached, TvdbPayload):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().seasons_extended(season_id)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def series_extended(self, series_id: int, meta: str = 'translations') -> TvdbSeriesData | None:
        key = f"tvdb:series:{series_id}:extended:{meta}"

        cached = await CacheService.get(key, TvdbSeriesData)
        if isinstance(cached, TvdbSeriesData):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().series_extended(series_id, meta)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def series_translations(self, series_id: int, language: str = 'zho') -> TvdbPayload | None:
        key = f"tvdb:series:{series_id}:translations:{language}"

        cached = await CacheService.get(key, TvdbPayload)
        if isinstance(cached, TvdbPayload):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().series_translations(series_id, language)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result
