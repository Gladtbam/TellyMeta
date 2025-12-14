import base64
import textwrap
from datetime import datetime, timedelta
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
from models.orm import LibraryBindingModel
from models.tvdb import TvdbData
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository
from services.user_service import Result

settings = get_settings()

class RequestState:
    def __init__(self, user_id: int, query: str):
        self.user_id = user_id
        self.query = query
        self.library_name: str | None = None
        self.timestamp = datetime.now()
        self.results: list[Any] = []

class RequestService:
    # Simple in-memory cache for user request sessions
    # Key: user_id, Value: RequestState
    _sessions: dict[int, RequestState] = {}

    def __init__(self, session: AsyncSession, app: FastAPI):
        self.session = session
        self.app = app
        self.config_repo = ConfigRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self._sonarr_client = app.state.sonarr_client
        self._radarr_client = app.state.radarr_client
        self._tmdb_client = app.state.tmdb_client
        self._tvdb_client = app.state.tvdb_client
        self.client: TelethonClientWarper = app.state.telethon_client

    @property
    def sonarr_client(self) -> SonarrClient:
        """Sonarr 客户端"""
        if self._sonarr_client is None:
            raise RuntimeError("Sonarr 客户端未配置")
        return self._sonarr_client
    @property
    def radarr_client(self) -> RadarrClient:
        """Radarr 客户端"""
        if self._radarr_client is None:
            raise RuntimeError("Radarr 客户端未配置")
        return self._radarr_client

    @property
    def tmdb_client(self) -> TmdbClient:
        """TMDB 客户端"""
        if self._tmdb_client is None:
            raise RuntimeError("TMDB 客户端未配置")
        return self._tmdb_client

    @property
    def tvdb_client(self) -> TvdbClient:
        """TVDB 客户端"""
        if self._tvdb_client is None:
            raise RuntimeError("TVDB 客户端未配置")
        return self._tvdb_client

    def _get_session(self, user_id: int) -> RequestState | None:
        """获取会话"""
        state = self._sessions.get(user_id)
        if state and datetime.now() - state.timestamp > timedelta(minutes=10):
            del self._sessions[user_id]
            return None
        return state

    def create_session(self, user_id: int, query: str) -> RequestState:
        """创建会话"""
        state = RequestState(user_id, query)
        self._sessions[user_id] = state
        return state

    async def get_bound_libraries(self) -> list[LibraryBindingModel]:
        """获取绑定的媒体库"""
        bindings = await self.config_repo.get_all_library_bindings()
        valid_bindings = []
        for binding in bindings.values():
            if binding.arr_type and binding.quality_profile_id and binding.root_folder:
                valid_bindings.append(binding)
        return valid_bindings

    async def start_request_flow(self, user_id: int, query: str) -> Result:
        """开始求片流程
        Args:
            user_id: 用户 ID
            query: 求片关键词
        Returns:
            Result: 求片流程结果
        """
        # Check if user has permission (must be Emby user)
        user = await self.telegram_repo.get_by_id(user_id)
        if not user or not user.emby:
            return Result(False, "您必须拥有已绑定的 Emby/Jellyfin 账户才能使用求片功能。")

        self.create_session(user_id, query)

        libraries = await self.get_bound_libraries()
        if not libraries:
            return Result(False, "管理员尚未配置媒体库绑定，无法使用求片功能。")
        keyboard = []
        # 对 user_id 进行编码以避免在回调中仅依赖 event.sender_id
        # 结构：req_lib_{lib_b64}_{user_id}

        for lib in libraries:
            lib_b64 = base64.b64encode(lib.library_name.encode('utf-8')).decode('utf-8')
            data_str = f"req_lib_{lib_b64}_{user_id}"
            keyboard.append([Button.inline(f"{lib.library_name} ({lib.arr_type})", data_str.encode('utf-8'))])
        keyboard.append([Button.inline("取消", b"req_cancel")])

        msg = f"您正在请求: **{query}**\n请选择要请求的媒体库："
        return Result(True, msg, keyboard=keyboard)

    async def process_library_selection(self, user_id: int, library_name: str) -> Result:
        """处理媒体库选择
        Args:
            user_id: 用户 ID
            library_name: 选择的媒体库名称
        Returns:
            Result: 处理结果
        """
        #依赖会话状态
        state = self._get_session(user_id)

        if not state:
            return Result(False, "会话已过期，请重新发起求片请求。")

        state.library_name = library_name
        binding = await self.config_repo.get_library_binding(library_name)

        results = []
        if binding.arr_type == 'sonarr':
            try:
                async for series in self.sonarr_client.lookup(state.query):
                    results.append(series)
                    if len(results) >= 5:
                        break
            except RuntimeError as e:
                return Result(False, f"搜索失败: {str(e)}")
        elif binding.arr_type == 'radarr':
            try:
                async for movie in self.radarr_client.lookup(state.query):
                    results.append(movie)
                    if len(results) >= 5:
                        break
            except RuntimeError as e:
                return Result(False, f"搜索失败: {str(e)}")

        state.results = results

        if not results:
            return Result(False, "未找到相关结果，请尝试更换关键词。")

        keyboard = []
        for idx, item in enumerate(results):
            title = item.title
            year = item.year or "未知年份"
            # Format: req_sel_{index}_{user_id} - also embed user_id here for consistency
            keyboard.append([Button.inline(f"{title} ({year})", f"req_sel_{idx}_{user_id}".encode('utf-8'))])
        keyboard.append([Button.inline("取消", b"req_cancel")])

        msg = f"在 **{library_name}** 中搜索 **{state.query}** 的结果："
        return Result(True, msg, keyboard=keyboard)

    async def _get_media_content(self, item: Any) -> tuple[str, str, str | None]:
        """获取媒体内容和海报链接，尝试获取中文信息"""
        title = item.title or "未知标题"
        overview = item.overview or ""
        poster_url = None

        # 查找海报
        if hasattr(item, 'images'):
            for img in item.images:
                if img.coverType == "poster" and img.remoteUrl:
                    poster_url = img.remoteUrl
                    break

        if not poster_url and hasattr(item, 'remotePoster') and item.remotePoster:
            poster_url = item.remotePoster

        # 如果是 Sonarr 条目 (通过 tvdbId 识别)，尝试获取中文信息
        if hasattr(item, 'tvdbId') and item.tvdbId:
             # 尝试 TVDB
            try:
                # 尝试获取中文翻译
                tvdb_resp = await self.tvdb_client.series_translations(item.tvdbId, language='zho')
                if tvdb_resp and isinstance(tvdb_resp.data, TvdbData):
                    if tvdb_resp.data.name:
                        title = tvdb_resp.data.name
                    if tvdb_resp.data.overview:
                        overview = tvdb_resp.data.overview
            except RuntimeError as e:
                logger.warning("无法获取 {} 的 TVDB 信息：{}", item.tvdbId, e)

            # 如果 TVDB 获取到了，就用 TVDB。如果没获取到，试 TMDB。
            if hasattr(item, 'tmdbId') and item.tmdbId:
                try:
                    tmdb_info = await self.tmdb_client.get_tv_details(item.tmdbId)
                    if tmdb_info:
                        if not overview and tmdb_info.overview:
                            overview = tmdb_info.overview
                except RuntimeError as e:
                    logger.warning("无法获取 {} 的 TMDB 信息：{}", item.tmdbId, e)

        return title, overview, poster_url

    async def process_media_selection(self, user_id: int, index: int) -> Result:
        """处理媒体选择
        Args:
            user_id: 用户 ID
            index: 选择的媒体索引
        Returns:
            Result: 处理结果
        """
        state = self._get_session(user_id)
        if not state or not state.library_name:
            return Result(False, "会话已过期，请重新发起求片请求。")

        if index >= len(state.results):
            return Result(False, "选择无效。")

        selected_media = state.results[index]

        topic_id_str = await self.config_repo.get_settings("requested_notify_topic")
        if not topic_id_str or topic_id_str == "未设置":
            return Result(False, "管理员未设置求片通知话题，无法提交请求。")

        topic_id = int(topic_id_str)

        user_name = await self.client.get_user_name(user_id)

        # 获取媒体信息 (Async)
        title, overview, poster_url = await self._get_media_content(selected_media)

        year = selected_media.year or ""
        short_overview = textwrap.shorten(overview or "无简介", width=200, placeholder="...")

        binding = await self.config_repo.get_library_binding(state.library_name)
        media_id = 0
        id_label = ""

        if binding.arr_type == 'sonarr':
            if hasattr(selected_media, 'tvdbId') and selected_media.tvdbId:
                media_id = selected_media.tvdbId
                id_label = "TVDB"
        elif binding.arr_type == 'radarr':
            if hasattr(selected_media, 'tmdbId') and selected_media.tmdbId:
                media_id = selected_media.tmdbId
                id_label = "TMDB"

        if not media_id:
            return Result(False, "无法获取有效的媒体 ID，无法提交请求。")

        lib_b64 = base64.b64encode(state.library_name.encode('utf-8')).decode('utf-8')

        # Callback data: req_ap_{lib_b64}_{id}
        approve_data = f"req_ap_{lib_b64}_{media_id}"
        deny_data = f"req_deny_{user_id}"

        if len(approve_data.encode('utf-8')) > 64:
            return Result(False, "错误：媒体库名称过长，无法生成批准按钮。请联系管理员修改媒体库名称。")

        buttons = [
            [Button.inline("✅ 批准", approve_data.encode('utf-8')), Button.inline("❌ 拒绝", deny_data.encode('utf-8'))]
        ]

        msg = textwrap.dedent(f"""\
            **🆕 新的求片请求**
            
            👤 **申请人**: [{user_name}](tg://user?id={user_id})
            🎬 **标题**: {title} ({year})
            📚 **媒体库**: {state.library_name}
            📝 **简介**: {short_overview}
            
            ID: {id_label}:{media_id}
        """)

        # 如果有海报，发送带图片的附件消息；否则发送纯文本
        if poster_url:
            await self.client.send_message(topic_id, msg, file=poster_url, buttons=buttons)
        else:
            await self.client.send_message(topic_id, msg, buttons=buttons)

        # Clean up session
        del self._sessions[user_id]

        return Result(True, "求片请求已提交，请等待管理员审核。")

    async def handle_approval(self, library_name: str, media_id: int) -> Result:
        """处理审批
        Args:
            library_name: 媒体库名称
            media_id: 媒体 ID
        Returns:
            Result: 处理结果
        """
        binding = await self.config_repo.get_library_binding(library_name)
        if not binding.arr_type:
            return Result(False, f"媒体库 {library_name} 配置无效。")
        if not binding.quality_profile_id or not binding.root_folder:
            return Result(False, f"媒体库 {library_name} 配置无效。")

        if binding.arr_type == 'sonarr':
            # Sonarr uses tvdb:id
            search_term = f"tvdb:{media_id}"

            try:
                # Use property for error checking
                async for series in self.sonarr_client.lookup(search_term):
                    # Configure series object for addition
                    series.qualityProfileId = binding.quality_profile_id
                    series.rootFolderPath = binding.root_folder

                    result_series = await self.sonarr_client.post_series(series)
                    if result_series:
                        return Result(True, f"已添加剧集: {result_series.title}")
                    else:
                        return Result(False, "添加剧集失败，可能已存在。")
            except RuntimeError as e:
                return Result(False, f"添加剧集失败: {str(e)}")

        elif binding.arr_type == 'radarr':
            # Radarr uses tmdb:id
            search_term = f"tmdb:{media_id}"
            try:
                # Use property for error checking
                async for movie in self.radarr_client.lookup(search_term):
                    movie.qualityProfileId = binding.quality_profile_id
                    movie.rootFolderPath = binding.root_folder

                    result_movie = await self.radarr_client.post_movie(movie)
                    if result_movie:
                        return Result(True, f"已添加电影: {result_movie.title}")

                    return Result(False, "添加电影失败，可能已存在。")
            except RuntimeError as e:
                return Result(False, f"添加电影失败: {str(e)}")

        return Result(False, "无法找到对应的媒体信息。")
