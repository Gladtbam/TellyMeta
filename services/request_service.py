import base64
import textwrap
from typing import Any

import httpx
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
from models.radarr import MovieResource
from models.sonarr import SeriesResource
from models.tvdb import TvdbData
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.notification_service import NotificationService
from services.user_service import Result

settings = get_settings()

class RequestService:
    def __init__(self, app: FastAPI, session: AsyncSession):
        self.config_repo = ConfigRepository(session)
        self.media_repo = MediaRepository(session)
        self.server_repo = ServerRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self.notification_service = NotificationService(app)
        self._sonarr_clients: dict[int, SonarrClient] = app.state.sonarr_clients
        self._radarr_clients: dict[int, RadarrClient] = app.state.radarr_clients
        self.tmdb_client: TmdbClient | None = app.state.tmdb_client
        self.tvdb_client: TvdbClient | None = app.state.tvdb_client
        self.client: TelethonClientWarper = app.state.telethon_client

    async def _get_client_by_library(self, library_name: str) -> tuple[SonarrClient | RadarrClient | None, int | None]:
        """根据库名获取对应的 Media Client 和 Server ID"""
        binding = await self.config_repo.get_library_binding(library_name)
        if not binding.server_id:
            return None, None

        client = self._sonarr_clients.get(binding.server_id) or self._radarr_clients.get(binding.server_id)
        return client, binding.server_id

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

    async def start_request_flow(self, user_id: int, request_cost: int) -> Result:
        user = await self.telegram_repo.get_or_create(user_id)
        if user.score < request_cost:
            return Result(False, f"您的积分不足，求片需要消耗 **{request_cost}** 积分，您当前仅有 **{user.score}** 积分。")

        bindings = await self.config_repo.get_all_library_bindings()
        valid_bindings = []

        for name, binding in bindings.items():
            if not (binding.server_id and binding.quality_profile_id and binding.root_folder):
                continue
            if self._sonarr_clients.get(binding.server_id) or self._radarr_clients.get(binding.server_id):
                valid_bindings.append(name)

        if not valid_bindings:
            return Result(False, "未配置任何可用于求片的媒体库，请联系管理员绑定 Sonarr/Radarr。")

        keyboard = []
        for name in valid_bindings:
            name_b64 = base64.b64encode(name.encode('utf-8')).decode('utf-8')
            keyboard.append([
                Button.inline(f"🔍 {name}", data=f"req_lib_{name_b64}_{user_id}".encode('utf-8'))
            ])

        msg = textwrap.dedent(f"""\
            📚 求片流程：
            1. 选择媒体库
            2. 搜索媒体
            3. 选择媒体
            4. 确认提交请求
            
            您当前积分：**{user.score}**
            求片消耗积分：**{request_cost}**
        """)
        return Result(True, msg, keyboard=keyboard)

    async def search_media(self, library_name: str, query: str) -> Result:
        if not query:
            return Result(False, "搜索关键词为空。")

        client, _ = await self._get_client_by_library(library_name)
        if not client:
            return Result(False, "该媒体库未绑定有效的媒体服务器。")

        results = []
        try:
            async for item in client.lookup(query):
                results.append(item)
                if len(results) >= 5:
                    break
        except httpx.HTTPError as e:
            logger.warning("Media search failed (HTTP): {}", e)
            return Result(False, "服务器连接失败，请稍后重试。")
        except Exception as e:
            logger.error("Media search failed (Unknown): {}", e)
            return Result(False, f"搜索失败: {str(e)}")

        if not results:
            return Result(False, "未找到相关结果，请尝试更换关键词。")

        keyboard = []
        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')

        for item in results:
            status_icon = ""
            if hasattr(item, 'id') and item.id:
                status_icon = "✅ "
            elif hasattr(item, 'added') and item.added:
                status_icon = "⏳ "

            year = getattr(item, 'year', "未知年份")
            media_id = getattr(item, 'tvdbId', getattr(item, 'tmdbId', 0))

            btn_text = f"{status_icon}{item.title} ({year})"
            callback_data = f"req_sel_{lib_b64}_{media_id}".encode('utf-8')

            keyboard.append([Button.inline(btn_text, data=callback_data)])

        keyboard.append([Button.inline("取消", b"req_cancel")])
        return Result(True, f"🔍 在 **{library_name}** 中搜索 **{query}** 的结果：", keyboard=keyboard)

    async def process_media_selection(self, user_id: int, library_name: str, media_id: int) -> Result:
        client, server_id = await self._get_client_by_library(library_name)
        if not client or not server_id:
            return Result(False, "服务不可用")

        existing_item = None
        try:
            if isinstance(client, SonarrClient):
                existing_item = await client.get_series_by_tvdb(media_id)
            elif isinstance(client, RadarrClient):
                existing_item = await client.get_movie_by_tmdb(media_id)
        except httpx.HTTPError as e:
            logger.debug("查重请求失败 (HTTP): {}", e)
        except Exception as e:
            logger.debug("查重请求失败 (Unknown): {}", e)

        if existing_item:
            return Result(False, f"✅ **{existing_item.title}** 已经在媒体库中了，无需重复请求。")

        prefix = "tvdb" if isinstance(client, SonarrClient) else "tmdb"
        selected_media = None
        try:
            async for item in client.lookup(f"{prefix}:{media_id}"):
                if item:
                    selected_media = item
                    break
        except httpx.HTTPError as e:
            return Result(False, f"获取媒体元数据失败 (HTTP): {e}")
        except Exception as e:
            return Result(False, f"获取媒体元数据失败: {e}")

        if not selected_media:
            return Result(False, "无法获取媒体详情。")

        title, overview, poster = await self._get_media_content(selected_media, client)

        server_info = await self.server_repo.get_by_id(server_id)
        server_name = server_info.name if server_info else "Unknown"
        year = getattr(selected_media, 'year', '')

        msg = textwrap.dedent(f"""\
            🎬 **{title}** ({year})
            
            {textwrap.shorten(overview, width=200, placeholder="...") if overview else '暂无简介'}

            📚 媒体库: {library_name}
            🖥️ 服务器: {server_name}
        """)

        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        keyboard = [
            [Button.inline("📤 确认提交请求", data=f"req_submit_{lib_b64}_{media_id}".encode('utf-8'))],
            [Button.inline("« 返回", data=b"req_cancel")]
        ]

        return Result(True, msg, keyboard=keyboard, extra_data=poster)

    async def submit_final_request(self, user_id: int, library_name: str, media_id: int, request_cost: int) -> Result:
        client, server_id = await self._get_client_by_library(library_name)
        if not client or not server_id:
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
        server_info = await self.server_repo.get_by_id(server_id)
        if not server_info:
            return Result(False, "关联的服务器实例不存在。")

        topic_id = server_info.request_notify_topic_id
        if not topic_id:
            return Result(False, f"管理员未设置服务器 **{server_info.name}** 的通知，无法提交请求。")

        title, overview, poster = await self._get_media_content(selected_media, client)

        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        buttons = [
            [
                Button.inline("✅ 批准", data=f"req_ap_{lib_b64}_{media_id}".encode('utf-8')),
                Button.inline("❌ 拒绝", data=f"req_deny_{user_id}".encode('utf-8'))
            ]
        ]

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
            server_name=server_info.name,
            overview=overview,
            prefix=prefix.upper()
        )

        # 扣除积分
        await self.telegram_repo.update_score(user_id, -request_cost)

        return Result(True, f"✅ 请求已成功提交！(已扣除 **{request_cost}** 积分)\n请耐心等待管理员审核。")

    async def handle_approval(self, library_name: str, media_id: int, approver_name: str = "管理员") -> Result:
        client, _ = await self._get_client_by_library(library_name)
        binding = await self.config_repo.get_library_binding(library_name)

        if not client or not binding.quality_profile_id or not binding.root_folder:
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
        except httpx.HTTPError as e:
            return Result(False, f"添加失败 (API错误): {e}")
        except Exception as e:
            return Result(False, f"添加失败: {e}")
