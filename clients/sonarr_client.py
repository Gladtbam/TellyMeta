from collections.abc import AsyncGenerator

import httpx
from pydantic import TypeAdapter

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.radarr import QualityProfileResource, RootFolderResource
from models.sonarr import AddSeriesOptions, EpisodeResource, SeriesResource

setting = get_settings()

class SonarrClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str, path_mappings: dict[str, str] | None = None) -> None:
        super().__init__(client)
        self.api_key = api_key
        self.path_mappings = path_mappings or {}

    async def _login(self) -> None:
        # Sonarr 使用 API Key 进行认证，无需登录
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

    async def lookup(self, term: str) -> AsyncGenerator[SeriesResource, None]:
        """根据电视剧名称查找 The TVDB 获取的剧集信息。
        Args:
            term (str): 电视剧名称。
        Returns:
            AsyncGenerator[SeriesResource, None]: 返回剧集信息的生成器。
        """
        url = "/api/v3/series/lookup"
        params = {'term': term}
        response = await self.get(url, params=params,
            parser=lambda data: TypeAdapter(list[SeriesResource]).validate_python(data))
        if response is None:
            return

        for serie in response:
            serie.path = self.to_local_path(serie.path)
            yield serie

    async def get_series_by_tvdb(self, tvdb_id: int) -> SeriesResource | None:
        """根据 TVDB ID 获取 Sonarr 中的剧集信息。
        Args:
            tvdb_id (int): TVDB 剧集 ID。
        Returns:
            SeriesResource | None: 返回剧集信息，如果查询失败则返回 None。
        """
        url = "/api/v3/series"
        params = {'tvdbId': tvdb_id, 'includeSeasonImages': 'true'}
        response = await self.get(url, params=params,
            parser=lambda data: TypeAdapter(list[SeriesResource]).validate_python(data))

        if response and response[0]:
            series = response[0]
            series.path = self.to_local_path(series.path)
            return series
        return None

    async def get_episode_by_series_id(self, series_id: int) -> list[EpisodeResource] | None:
        """根据剧集 ID 获取 Sonarr 中的剧集的所有剧集信息。
        Args:
            series_id (int): 剧集 ID。
        Returns:
            list[EpisodeResource] | None: 返回剧集的所有剧集信息的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/episode"
        params = {
            'seriesId': series_id,
            'includeSeries': 'true',
            'includeEpisodeFile': 'true',
            'includeImages': 'true'
            }
        episodes = await self.get(url, params=params,
            parser=lambda data: TypeAdapter(list[EpisodeResource]).validate_python(data))

        if episodes:
            for ep in episodes:
                if ep.series:
                    ep.series.path = self.to_local_path(ep.series.path)
                if ep.episodeFile:
                    ep.episodeFile.path = self.to_local_path(ep.episodeFile.path)
        return episodes

    async def post_series(self, series_resource: SeriesResource) -> SeriesResource | None:
        """添加新剧集到 Sonarr。
        Args:
            series_data (dict): 包含剧集信息的字典。
        Returns:
            SeriesResource | None: 返回添加的剧集信息，如果添加失败则返回 None。
        """
        url = "/api/v3/series"
        series_resource.addOptions = AddSeriesOptions(
            ignoreEpisodesWithFiles = True,
            ignoreEpisodesWithoutFiles = False,
            monitor = "all",
            searchForMissingEpisodes = True,
            searchForCutoffUnmetEpisodes = False
        )
        series_resource.monitored = True
        series_resource.seasonFolder = True
        return await self.post(url,
            json=series_resource.model_dump(exclude_unset=True),
            response_model=SeriesResource)

    async def get_root_folders(self) -> list[RootFolderResource] | None:
        """获取 Sonarr 的根文件夹列表。
        Returns:
            list[RootFolderResource] | None: 返回根文件夹路径的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/rootfolder"
        return await self.get(url,
            parser=lambda data: TypeAdapter(list[RootFolderResource]).validate_python(data))

    async def get_quality_profiles(self) -> list[QualityProfileResource] | None:
        """获取 Sonarr 的质量配置文件列表。
        Returns:
            list[QualityProfileResource] | None: 返回质量配置文件的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/qualityprofile"
        return await self.get(url,
            parser=lambda data: TypeAdapter(list[QualityProfileResource]).validate_python(data))
