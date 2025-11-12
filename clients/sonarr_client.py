import httpx
from pydantic import TypeAdapter

from clients.base_client import AuthenticatedClient
from core.config import get_settings
from models.radarr import QualityProfileResource, RootFolderResource
from models.sonarr import EpisodeResource, SeriesResource

setting = get_settings()

class SonarrClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        super().__init__(client)
        self.api_key = api_key

    async def _login(self) -> None:
        # Sonarr 使用 API Key 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "X-Api-Key": self.api_key,
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    async def lookup_by_tvdb(self, tvdb_id: int) -> SeriesResource | None:
        """根据 TVDB ID 查找 The TVDB 获取的剧集信息。
        Args:
            tvdb_id (int): TVDB 剧集 ID。
        Returns:
            dict | None: 返回剧集信息的字典，如果查询失败则返回 None。
        """
        url = "/api/v3/series/lookup/tvdb"
        params = {'term': f'tvdb:{tvdb_id}'}
        response = await self.get(url, params=params)
        if not response:
            return None
        return TypeAdapter(list[SeriesResource]).validate_python(response.json())[0]

    async def get_series_by_tvdb(self, tvdb_id: int) -> SeriesResource | None:
        """根据 TVDB ID 获取 Sonarr 中的剧集信息。
        Args:
            tvdb_id (int): TVDB 剧集 ID。
        Returns:
            dict | None: 返回剧集信息的字典，如果查询失败则返回 None。
        """
        url = "/api/v3/series"
        params = {'tvdbId': tvdb_id, 'includeSeasonImages': 'true'}
        response = await self.get(url, params=params)
        if not response:
            return None
        return TypeAdapter(list[SeriesResource]).validate_python(response.json())[0]

    async def get_episode_by_series_id(self, series_id: int) -> list[EpisodeResource] | None:
        """根据剧集 ID 获取 Sonarr 中的剧集的所有剧集信息。
        Args:
            series_id (int): 剧集 ID。
        Returns:
            list[dict] | None: 返回剧集的所有剧集信息的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/episode"
        params = {
            'seriesId': series_id,
            'includeSeries': 'true',
            'includeEpisodeFile': 'true',
            'includeImages': 'true'
            }
        response = await self.get(url, params=params)
        if not response:
            return None
        return TypeAdapter(list[EpisodeResource]).validate_python(response.json())

    async def post_series(self, series_resource: SeriesResource) -> SeriesResource | None:
        """添加新剧集到 Sonarr。
        Args:
            series_data (dict): 包含剧集信息的字典。
        Returns:
            dict | None: 返回添加的剧集信息的字典，如果添加失败则返回 None。
        """
        url = "/api/v3/series"
        response = await self.post(url, json=series_resource.model_dump(), response_model=SeriesResource)
        return response if isinstance(response, SeriesResource) else None

    async def get_root_folders(self) -> None | list[RootFolderResource]:
        """获取 Sonarr 的根文件夹列表。
        Returns:
            list[str] | None: 返回根文件夹路径的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/rootfolder"
        response = await self.get(url)
        if not response:
            return None
        return TypeAdapter(list[RootFolderResource]).validate_python(response.json())

    async def get_quality_profiles(self) -> None | list[QualityProfileResource]:
        """获取 Sonarr 的质量配置文件列表。
        Returns:
            list[dict] | None: 返回质量配置文件的列表，如果查询失败则返回 None。
        """
        url = "/api/v3/qualityprofile"
        response = await self.get(url)
        if not response:
            return None
        return TypeAdapter(list[QualityProfileResource]).validate_python(response.json())
