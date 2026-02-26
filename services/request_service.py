import base64
from typing import Any

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.events import NotificationEvent
from models.orm import LibraryBinding
from models.radarr import MovieResource
from models.sonarr import SeriesResource
from models.tvdb import TvdbData
from repositories.binding_repo import BindingRepository
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services import media_service
from services.notification_service import NotificationService
from services.user_service import Result

settings = get_settings()

class RequestService:
    def __init__(self, app: FastAPI, session: AsyncSession):
        self.config_repo = ConfigRepository(session)
        self.binding_repo = BindingRepository(session)
        self.media_repo = MediaRepository(session)
        self.server_repo = ServerRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self.notification_service = NotificationService(app)
        self._sonarr_clients: dict[int, SonarrClient] = app.state.sonarr_clients
        self._radarr_clients: dict[int, RadarrClient] = app.state.radarr_clients
        self.tmdb_client: TmdbClient | None = app.state.tmdb_client
        self.tvdb_client: TvdbClient | None = app.state.tvdb_client
        self.client: TelethonClientWarper = app.state.telethon_client

    async def _get_client_by_library(self, media_server_id: int, library_name: str) -> tuple[SonarrClient | RadarrClient | None, LibraryBinding | None]:
        """根据媒体服务器ID+库名获取对应的 Arr Client 和绑定信息"""
        binding = await self.binding_repo.get_by_key(media_server_id, library_name)
        if not binding:
            return None, None

        client = self._sonarr_clients.get(binding.arr_id) or self._radarr_clients.get(binding.arr_id)
        return client, binding

    async def _get_media_content(self, item: Any, client: Any) -> tuple[str, str, str | None]:
        """获取媒体的中文标题、简介和海报"""
        title = getattr(item, 'title', "未知标题")
        overview = getattr(item, 'overview', "") or ""
        poster_url = self._extract_poster(item)

        try:
            if isinstance(client, SonarrClient):
                title, overview = await self._fetch_series_metadata(item, title, overview)
            elif isinstance(client, RadarrClient):
                title, overview = await self._fetch_movie_metadata(item, title, overview)
        except Exception as e:
            logger.debug(f"元数据增强失败，降级使用原始数据: {e}")

        return title, overview, poster_url

    def _extract_poster(self, item: Any) -> str | None:
        """从 Sonarr/Radarr 对象中提取海报"""
        if hasattr(item, 'images') and item.images:
            for img in item.images:
                if getattr(img, 'coverType', '') == "poster" and getattr(img, 'remoteUrl', None):
                    return img.remoteUrl

        if hasattr(item, 'remotePoster') and item.remotePoster:
            return item.remotePoster

        return None

    async def _fetch_series_metadata(self, item: Any, default_title: str, default_overview: str) -> tuple[str, str]:
        """获取剧集元数据 (TVDB -> TMDB)"""
        title = default_title
        overview = default_overview

        tvdb_id = getattr(item, 'tvdbId', None)
        tmdb_id = getattr(item, 'tmdbId', None)

        if tvdb_id and self.tvdb_client:
            try:
                tvdb_resp = await self.tvdb_client.series_translations(tvdb_id, language='zho')
                if tvdb_resp and isinstance(tvdb_resp.data, TvdbData):
                    if tvdb_resp.data.name:
                        title = tvdb_resp.data.name
                    if tvdb_resp.data.overview:
                        overview = tvdb_resp.data.overview
            except Exception as e:
                logger.debug(f"TVDB 查找失败 ({tvdb_id}): {e}")

        if not overview and tmdb_id and self.tmdb_client:
            try:
                tmdb_info = await self.tmdb_client.get_tv_series_details(tmdb_id)
                if tmdb_info and tmdb_info.overview:
                    overview = tmdb_info.overview
            except Exception as e:
                logger.debug(f"TMDB TV 查找失败 ({tmdb_id}): {e}")

        return title, overview

    async def _fetch_movie_metadata(self, item: Any, default_title: str, default_overview: str) -> tuple[str, str]:
        """获取电影元数据 (TMDB)"""
        title = default_title
        overview = default_overview

        tmdb_id = getattr(item, 'tmdbId', None)

        if tmdb_id and self.tmdb_client:
            try:
                tmdb_movie = await self.tmdb_client.get_movie_details(tmdb_id)

                if tmdb_movie:
                    if tmdb_movie.title:
                        title = tmdb_movie.title
                    if tmdb_movie.overview:
                        overview = tmdb_movie.overview
            except Exception as e:
                logger.debug(f"TMDB Movie 查找失败 ({tmdb_id}): {e}")

        return title, overview

    async def submit_final_request(self, user_id: int, media_server_id: int, library_name: str, media_id: int, request_cost: int) -> Result:
        client, binding = await self._get_client_by_library(media_server_id, library_name)
        if not client or not binding:
            return Result(False, "服务不可用")

        prefix = "tvdb" if isinstance(client, SonarrClient) else "tmdb"
        selected_media = None
        async for item in client.lookup(f"{prefix}:{media_id}"):
            if item:
                selected_media = item
                break

        if not selected_media:
            return Result(False, "获取媒体信息失败")

        user_name = await self.client.get_user_name(user_id)

        # 使用绑定的 arr_id 获取 Sonarr/Radarr 服务器信息用于通知
        arr_server = await self.server_repo.get_by_id(binding.arr_id)
        if not arr_server:
            return Result(False, "关联的 Arr 服务器实例不存在。")

        topic_id = arr_server.request_notify_topic_id
        if not topic_id:
            return Result(False, f"管理员未设置服务器 **{arr_server.name}** 的通知，无法提交请求。")

        title, overview, poster = await self._get_media_content(selected_media, client)

        # 回调数据格式: req_ap_{media_server_id}_{lib_b64}_{media_id}
        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        buttons = [
            [
                Button.inline("✅ 批准", data=f"req_ap_{media_server_id}_{lib_b64}_{media_id}".encode('utf-8')),
                Button.inline("❌ 拒绝", data=f"req_deny_{user_id}".encode('utf-8'))
            ]
        ]

        media_server = await self.server_repo.get_by_id(media_server_id)

        await self.notification_service.send_to_topic(
            topic_id=topic_id,
            event_type=NotificationEvent.REQUEST_SUBMIT,
            image=poster,
            buttons=buttons,
            # 模板变量
            user_name=user_name,
            user_id=user_id,
            media_title=title,
            media_year=getattr(selected_media, 'year', '未知'),
            tmdb_id=media_id,
            server_name=getattr(media_server, 'name', '未知'),
            overview=overview,
            prefix=prefix.upper()
        )

        # 扣除积分
        await self.telegram_repo.update_score(user_id, -request_cost)

        return Result(True, f"✅ 请求已成功提交！(已扣除 **{request_cost}** 积分)\n请耐心等待管理员审核。")

    async def handle_approval(self, media_server_id: int, library_name: str, media_id: int, approver_name: str = "管理员") -> Result:
        client, binding = await self._get_client_by_library(media_server_id, library_name)

        if not client or not binding:
            return Result(False, f"媒体库 {library_name} 配置无效或服务未连接。")

        prefix = "tvdb" if isinstance(client, SonarrClient) else "tmdb"

        target_item = None
        async for item in client.lookup(f"{prefix}:{media_id}"):
            if item:
                target_item = item
                break

        if not target_item:
            return Result(False, "无法从服务器获取媒体元数据。")

        target_item.qualityProfileId = binding.quality_profile_id
        target_item.rootFolderPath = binding.root_folder

        if hasattr(target_item, 'monitored'):
            target_item.monitored = True

        try:
            result = None
            if isinstance(client, SonarrClient) and isinstance(target_item, SeriesResource):
                result = await client.post_series(target_item)
            elif isinstance(client, RadarrClient) and isinstance(target_item, MovieResource):
                result = await client.post_movie(target_item)

            if result:
                return Result(True, f"✅ 已批准并添加 **{result.title}** (操作人: {approver_name})")
            return Result(False, "添加失败，接口未返回确认数据。")
        except Exception as e:
            return Result(False, f"添加失败: {e}")

    async def get_request_cost(self) -> int:
        """获取求片消耗积分"""
        renew_score = await self.telegram_repo.get_renew_score()
        return int(renew_score * 0.1)

    async def get_requestable_libraries(self, media_server_id: int) -> list[dict]:
        """API: 获取可求片的库列表（按 Emby/Jellyfin 实例筛选）"""
        bindings = await self.binding_repo.get_by_media_id(media_server_id)
        valid_bindings = []
        for binding in bindings:
            client = self._sonarr_clients.get(binding.arr_id) or self._radarr_clients.get(binding.arr_id)
            if client:
                type_ = "sonarr" if isinstance(client, SonarrClient) else "radarr"
                valid_bindings.append({"name": binding.library_name, "type": type_})
        return valid_bindings

    async def search_media_items(self, media_server_id: int, library_name: str, query: str) -> list[dict]:
        """API: 搜索媒体"""
        client, _ = await self._get_client_by_library(media_server_id, library_name)
        if not client:
            return []

        results = []
        try:
            async for item in client.lookup(query):
                results.append(item)
                if len(results) >= 20: # Limit for API
                    break
        except Exception as e:
            logger.error("API Search failed: {}", e)
            return []

        data = []
        for item in results:
            media_id = getattr(item, 'tvdbId', getattr(item, 'tmdbId', 0))
            poster = self._extract_poster(item)
            year = getattr(item, 'year', 0)
            status = 'new'

            item_id = getattr(item, 'id', None)

            if isinstance(item_id, int) and item_id > 0:
                status = 'existing'

            data.append({
                "media_id": media_id,
                "title": item.title,
                "year": year,
                "poster": poster,
                "status": status,
                "overview": getattr(item, 'overview', '')
            })
        return data

    async def submit_request_api(self, user_id: int, media_server_id: int, library_name: str, media_id: int) -> Result:
        """API: 提交求片"""
        cost = await self.get_request_cost()
        user = await self.telegram_repo.get_or_create(user_id)

        if user.score < cost:
            return Result(False, f"积分不足，需要 {cost} 积分，您当前仅有 {user.score} 积分。")

        return await self.submit_final_request(user_id, media_server_id, library_name, media_id, cost)
