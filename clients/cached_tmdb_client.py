import httpx
from loguru import logger

from clients.tmdb_client import TmdbClient
from models.tmdb import TmdbFindPayload, TmdbMovie, TmdbSeason, TmdbTvSeries
from services.cache_service import CacheService


class CachedTmdbClient(TmdbClient):
    """带缓存的 TMDB 客户端"""

    def __init__(self, client: httpx.AsyncClient, api_key: str, cache_ttl: int = 86400 * 7):
        super().__init__(client, api_key)
        self.cache_ttl = cache_ttl  # 默认缓存 7 天

    async def find_info_by_external_id(
        self,
        external_source: str,
        external_id: str
    ) -> TmdbFindPayload | None:
        key = f"tmdb:find:{external_source}:{external_id}"

        cached = await CacheService.get(key, TmdbFindPayload)
        if isinstance(cached, TmdbFindPayload):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().find_info_by_external_id(external_source, external_id)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def get_tv_series_details(self, tmdb_id: int) -> TmdbTvSeries | None:
        key = f"tmdb:tv:{tmdb_id}"

        cached = await CacheService.get(key, TmdbTvSeries)
        if isinstance(cached, TmdbTvSeries):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().get_tv_series_details(tmdb_id)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def get_tv_seasons_details(self, tmdb_id: int, season_number: int) -> TmdbSeason | None:
        key = f"tmdb:tv:{tmdb_id}:season:{season_number}"

        cached = await CacheService.get(key, TmdbSeason)
        if isinstance(cached, TmdbSeason):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().get_tv_seasons_details(tmdb_id, season_number)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result

    async def get_movie_details(self, tmdb_id: int) -> TmdbMovie | None:
        key = f"tmdb:movie:{tmdb_id}"

        cached = await CacheService.get(key, TmdbMovie)
        if isinstance(cached, TmdbMovie):
            logger.debug("缓存命中：{}", key)
            return cached

        result = await super().get_movie_details(tmdb_id)

        if result:
            await CacheService.set(key, result, self.cache_ttl)

        return result
