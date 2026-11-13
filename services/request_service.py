import base64
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from httpx2 import HTTPError
from loguru import logger
from sqlalchemy.exc import IntegrityError
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
from repositories.request_repo import RequestRepository
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
        self.request_repo = RequestRepository(session)
        self.server_repo = ServerRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self.notification_service = NotificationService(app)
        self._sonarr_clients: dict[int, SonarrClient] = app.state.sonarr_clients
        self._radarr_clients: dict[int, RadarrClient] = app.state.radarr_clients
        self.tmdb_client: TmdbClient | None = app.state.tmdb_client
        self.tvdb_client: TvdbClient | None = app.state.tvdb_client
        self.client: TelethonClientWarper = app.state.telethon_client

    async def _get_client_by_library(
        self, media_server_id: int, library_name: str
    ) -> tuple[SonarrClient | RadarrClient | None, LibraryBinding | None]:
        """根据媒体服务器ID+库名获取对应的 Arr Client 和绑定信息"""
        binding = await self.binding_repo.get_by_key(media_server_id, library_name)
        if not binding:
            return None, None

        client = self._sonarr_clients.get(binding.arr_id) or self._radarr_clients.get(
            binding.arr_id
        )
        return client, binding

    async def _get_media_content(
        self, item: Any, client: Any
    ) -> tuple[str, str, str | None]:
        """获取媒体的中文标题、简介和海报"""
        title = getattr(item, "title", "未知标题")
        overview = getattr(item, "overview", "") or ""
        poster_url = self._extract_poster(item)

        try:
            if isinstance(client, SonarrClient):
                title, overview = await self._fetch_series_metadata(
                    item, title, overview
                )
            elif isinstance(client, RadarrClient):
                title, overview = await self._fetch_movie_metadata(
                    item, title, overview
                )
        except Exception as e:
            logger.debug(f"元数据增强失败，降级使用原始数据: {e}")

        return title, overview, poster_url

    def _extract_poster(self, item: Any) -> str | None:
        """从 Sonarr/Radarr 对象中提取海报"""
        if hasattr(item, "images") and item.images:
            for img in item.images:
                if getattr(img, "coverType", "") == "poster" and getattr(
                    img, "remoteUrl", None
                ):
                    return img.remoteUrl

        if hasattr(item, "remotePoster") and item.remotePoster:
            return item.remotePoster

        return None

    async def _fetch_series_metadata(
        self, item: Any, default_title: str, default_overview: str
    ) -> tuple[str, str]:
        """获取剧集元数据 (TVDB -> TMDB)"""
        title = default_title
        overview = default_overview

        tvdb_id = getattr(item, "tvdbId", None)
        tmdb_id = getattr(item, "tmdbId", None)

        if tvdb_id and self.tvdb_client:
            try:
                tvdb_resp = await self.tvdb_client.series_translations(
                    tvdb_id, language="zho"
                )
                if tvdb_resp and isinstance(tvdb_resp.data, TvdbData):
                    if tvdb_resp.data.name:
                        title = tvdb_resp.data.name
                    if tvdb_resp.data.overview:
                        overview = tvdb_resp.data.overview
            except (HTTPError, ValueError, TypeError) as e:
                logger.debug(f"TVDB 查找失败 ({tvdb_id}): {e}")

        if not overview and tmdb_id and self.tmdb_client:
            try:
                tmdb_info = await self.tmdb_client.get_tv_series_details(tmdb_id)
                if tmdb_info and tmdb_info.overview:
                    overview = tmdb_info.overview
            except (HTTPError, ValueError, TypeError) as e:
                logger.debug(f"TMDB TV 查找失败 ({tmdb_id}): {e}")

        return title, overview

    async def _fetch_movie_metadata(
        self, item: Any, default_title: str, default_overview: str
    ) -> tuple[str, str]:
        """获取电影元数据 (TMDB)"""
        title = default_title
        overview = default_overview

        tmdb_id = getattr(item, "tmdbId", None)

        if tmdb_id and self.tmdb_client:
            try:
                tmdb_movie = await self.tmdb_client.get_movie_details(tmdb_id)

                if tmdb_movie:
                    if tmdb_movie.title:
                        title = tmdb_movie.title
                    if tmdb_movie.overview:
                        overview = tmdb_movie.overview
            except (HTTPError, ValueError, TypeError) as e:
                logger.debug(f"TMDB Movie 查找失败 ({tmdb_id}): {e}")

        return title, overview

    async def submit_final_request(
        self,
        user_id: int,
        media_server_id: int,
        library_name: str,
        media_id: int,
        request_cost: int,
    ) -> Result:
        """提交求片申请：去重校验 -> 锁定并预留积分 -> 发送管理员通知（失败则回滚释放）"""
        client, binding = await self._get_client_by_library(
            media_server_id, library_name
        )
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

        # 同一目标 Arr 实例已有该媒体时直接拒绝，不预留积分
        if getattr(selected_media, "id", None):
            return Result(
                False,
                "目标媒体库对应的 Arr 实例已存在该媒体，无需重复申请。",
            )

        # 同一目标 Arr 实例已有待审核求片时直接拒绝，不预留积分
        arr_id = binding.arr_id
        if await self.request_repo.has_pending_for_arr(arr_id, prefix, media_id):
            return Result(
                False,
                "该媒体在目标 Arr 实例已有待审核的求片申请，请勿重复提交。",
            )

        # 使用绑定的 arr_id 获取 Sonarr/Radarr 服务器信息用于通知
        arr_server = await self.server_repo.get_by_id(arr_id)
        if not arr_server:
            return Result(False, "关联的 Arr 服务器实例不存在。")

        topic_id = arr_server.request_notify_topic_id
        if not topic_id:
            return Result(
                False,
                f"管理员未设置服务器 **{arr_server.name}** 的通知，无法提交请求。",
            )

        title, overview, poster = await self._get_media_content(selected_media, client)
        media_server = await self.server_repo.get_by_id(media_server_id)
        media_server_name = getattr(media_server, "name", "未知")
        user_name = await self.client.get_user_name(user_id)

        # 按提交时费用固定本次价格并原子预留积分，后续动态调价不追补或改扣
        fixed_cost = max(0, int(request_cost))
        if not await self.telegram_repo.deduct_score_if_sufficient(user_id, fixed_cost):
            user = await self.telegram_repo.get_or_create(user_id)
            return Result(
                False,
                f"积分不足，需要 {fixed_cost} 积分，您当前仅有 {user.score} 积分。",
            )

        try:
            req = await self.request_repo.create_request(
                user_id=user_id,
                media_server_id=media_server_id,
                arr_id=arr_id,
                library_name=library_name,
                media_type=prefix,
                media_id=media_id,
                title=title,
                reserved_cost=fixed_cost,
            )
        except IntegrityError:
            # 并发场景下触发同 Arr 实例同媒体 pending 唯一索引，退还预留积分
            await self.request_repo.session.rollback()
            if fixed_cost > 0:
                await self.telegram_repo.update_score(user_id, fixed_cost)
            return Result(
                False,
                "该媒体在目标 Arr 实例已有待审核的求片申请，请勿重复提交。",
            )

        req_id = req.id
        buttons = [
            [
                Button.inline("✅ 批准", data=f"req_ap_{req_id}".encode("utf-8")),
                Button.inline("❌ 拒绝", data=f"req_deny_{req_id}".encode("utf-8")),
            ]
        ]

        sent_msg = await self.notification_service.send_to_topic(
            topic_id=topic_id,
            event_type=NotificationEvent.REQUEST_SUBMIT,
            image=poster,
            buttons=buttons,
            # 模板变量
            user_name=user_name,
            user_id=user_id,
            media_title=title,
            media_year=getattr(selected_media, "year", "未知"),
            tmdb_id=media_id,
            server_name=media_server_name,
            overview=overview,
            prefix=prefix.upper(),
        )

        # 只有通知成功送达管理员话题，提交才算成功；失败则回滚状态并释放全部预留积分
        if not sent_msg:
            await self.request_repo.atomic_transition(
                req_id,
                from_status="pending",
                to_status="failed",
                cost_status="released",
                fail_reason="管理员话题通知发送失败",
            )
            if fixed_cost > 0:
                await self.telegram_repo.update_score(user_id, fixed_cost)
            logger.warning(
                "求片通知发送失败，已释放预留积分: request_id={}, user_id={}, cost={}",
                req_id,
                user_id,
                fixed_cost,
            )
            return Result(
                False,
                "❌ 求片通知发送失败，预留积分已退回，请稍后重试。",
            )

        msg_id = getattr(sent_msg, "id", None)
        if msg_id:
            await self.request_repo.update_topic_message_id(req_id, msg_id)

        logger.info(
            "求片提交成功并预留积分: request_id={}, user_id={}, arr_id={}, media={}:{}, reserved_cost={}",
            req_id,
            user_id,
            arr_id,
            prefix,
            media_id,
            fixed_cost,
        )
        return Result(
            True,
            f"✅ 请求已成功提交！(已预留 **{fixed_cost}** 积分，请求编号 #{req_id})\n请耐心等待管理员审核。",
        )

    async def _add_media_to_arr(
        self, media_server_id: int, library_name: str, media_id: int
    ) -> tuple[bool, str, str]:
        """向目标 Arr 实例添加媒体，返回 (是否成功, 媒体标题或错误信息, 详情)"""
        client, binding = await self._get_client_by_library(
            media_server_id, library_name
        )

        if not client or not binding:
            return False, f"媒体库 {library_name} 配置无效或服务未连接。", ""

        prefix = "tvdb" if isinstance(client, SonarrClient) else "tmdb"

        target_item = None
        async for item in client.lookup(f"{prefix}:{media_id}"):
            if item:
                target_item = item
                break

        if not target_item:
            return False, "无法从服务器获取媒体元数据。", ""

        target_item.qualityProfileId = binding.quality_profile_id
        target_item.rootFolderPath = binding.root_folder

        if hasattr(target_item, "monitored"):
            target_item.monitored = True

        try:
            result = None
            if isinstance(client, SonarrClient) and isinstance(
                target_item, SeriesResource
            ):
                result = await client.post_series(target_item)
            elif isinstance(client, RadarrClient) and isinstance(
                target_item, MovieResource
            ):
                result = await client.post_movie(target_item)

            if result:
                return True, getattr(result, "title", str(media_id)), ""
            return False, "添加失败，接口未返回确认数据。", ""
        except Exception as e:
            return False, f"添加失败: {e}", str(e)

    async def handle_approval(
        self,
        media_server_id: int,
        library_name: str,
        media_id: int,
        approver_name: str = "管理员",
    ) -> Result:
        """兼容旧版无 request_id 回调的直接审批方法"""
        ok, msg, _ = await self._add_media_to_arr(
            media_server_id, library_name, media_id
        )
        if ok:
            return Result(
                True,
                f"✅ 已批准并添加 **{msg}** (操作人: {approver_name})",
            )
        return Result(False, msg)

    async def handle_approval_by_request_id(
        self, request_id: int, approver_name: str = "管理员"
    ) -> Result:
        """按请求 ID 原子审批求片：成功加入 Arr 后结算预留积分，失败则全额释放"""
        req = await self.request_repo.get_by_id(request_id)
        if not req:
            return Result(False, "求片记录不存在。")

        if req.status != "pending":
            return Result(
                False,
                f"该求片请求状态为 {req.status}，无法重复批准。",
            )

        now = datetime.now()
        if req.expires_at <= now:
            # 已超期但定时任务尚未扫描到时，在此直接触发超时释放，禁止再批准
            expired_req = await self.request_repo.atomic_transition(
                request_id,
                from_status="pending",
                to_status="expired",
                cost_status="released",
                fail_reason="待审核超过7天自动取消",
                now=now,
            )
            if expired_req and expired_req.reserved_cost > 0:
                await self.telegram_repo.update_score(
                    expired_req.user_id, expired_req.reserved_cost
                )
            return Result(
                False,
                "该求片请求已超过 7 天有效期并自动取消，已释放预留积分，无法批准。",
            )

        # 原子抢占为 processing，防止并发重复审批或与撤销/超时并发冲突
        claimed = await self.request_repo.atomic_transition(
            request_id,
            from_status="pending",
            to_status="processing",
            cost_status="reserved",
            approver_name=approver_name,
            require_unexpired=True,
            now=now,
        )
        if not claimed:
            return Result(False, "该求片请求已被处理或已失效，不能重复操作。")

        ok, msg, _ = await self._add_media_to_arr(
            claimed.media_server_id, claimed.library_name, claimed.media_id
        )
        if ok:
            await self.request_repo.atomic_transition(
                request_id,
                from_status="processing",
                to_status="approved",
                cost_status="settled",
                approver_name=approver_name,
            )
            logger.info(
                "求片审批成功并结算积分: request_id={}, user_id={}, settled_cost={}, approver={}",
                request_id,
                claimed.user_id,
                claimed.reserved_cost,
                approver_name,
            )
            return Result(
                True,
                f"✅ 已批准并添加 **{msg}**（结算积分: {claimed.reserved_cost}，操作人: {approver_name}）",
            )

        # Arr 添加失败时释放全部预留积分，不发生该次消费
        await self.request_repo.atomic_transition(
            request_id,
            from_status="processing",
            to_status="failed",
            cost_status="released",
            approver_name=approver_name,
            fail_reason=msg[:500],
        )
        if claimed.reserved_cost > 0:
            await self.telegram_repo.update_score(
                claimed.user_id, claimed.reserved_cost
            )
        logger.warning(
            "Arr 添加失败，已释放预留积分: request_id={}, user_id={}, released_cost={}, reason={}",
            request_id,
            claimed.user_id,
            claimed.reserved_cost,
            msg,
        )
        return Result(
            False,
            f"❌ {msg}（已释放预留积分 {claimed.reserved_cost}）",
        )

    async def handle_rejection(
        self, request_id: int, approver_name: str = "管理员"
    ) -> Result:
        """管理员拒绝求片：原子更新状态并全额释放预留积分"""
        updated = await self.request_repo.atomic_transition(
            request_id,
            from_status="pending",
            to_status="rejected",
            cost_status="released",
            approver_name=approver_name,
            fail_reason="管理员拒绝",
        )
        if not updated:
            req = await self.request_repo.get_by_id(request_id)
            if not req:
                return Result(False, "求片记录不存在。")
            return Result(
                False,
                f"该求片请求当前状态为 {req.status}，无法重复操作。",
            )

        if updated.reserved_cost > 0:
            await self.telegram_repo.update_score(
                updated.user_id, updated.reserved_cost
            )
        logger.info(
            "求片已被拒绝并释放预留积分: request_id={}, user_id={}, released_cost={}, approver={}",
            request_id,
            updated.user_id,
            updated.reserved_cost,
            approver_name,
        )
        return Result(
            True,
            f"❌ 已拒绝求片请求 #{request_id}（操作人: {approver_name}），已释放预留积分 {updated.reserved_cost}。",
        )

    async def cancel_request(self, request_id: int, user_id: int) -> Result:
        """申请人撤销待审核求片：仅限本人且处于 pending 状态，撤销后全额释放预留积分"""
        req = await self.request_repo.get_by_id(request_id)
        if not req:
            return Result(False, "求片记录不存在。")
        if req.user_id != user_id:
            return Result(False, "无权撤销他人的求片请求。")

        updated = await self.request_repo.atomic_transition(
            request_id,
            from_status="pending",
            to_status="canceled",
            cost_status="released",
            user_id=user_id,
            fail_reason="申请人主动撤销",
        )
        if not updated:
            latest = await self.request_repo.get_by_id(request_id)
            status = latest.status if latest else "unknown"
            return Result(False, f"当前状态（{status}）不可撤销。")

        if updated.reserved_cost > 0:
            await self.telegram_repo.update_score(
                updated.user_id, updated.reserved_cost
            )
        logger.info(
            "申请人已撤销求片并释放积分: request_id={}, user_id={}, released_cost={}",
            request_id,
            updated.user_id,
            updated.reserved_cost,
        )
        return Result(
            True,
            f"已撤销求片请求 #{request_id}，已释放预留积分 {updated.reserved_cost}。",
        )

    async def expire_pending_requests(self, now: datetime | None = None) -> int:
        """超时清理：自动取消待审核满 7 天的求片请求并释放全部预留积分"""
        current_time = now or datetime.now()
        expired_list = await self.request_repo.find_expired_pending(now=current_time)
        expired_count = 0
        for req in expired_list:
            updated = await self.request_repo.atomic_transition(
                req.id,
                from_status="pending",
                to_status="expired",
                cost_status="released",
                fail_reason="待审核超过7天自动取消",
                now=current_time,
            )
            if not updated:
                continue
            if updated.reserved_cost > 0:
                await self.telegram_repo.update_score(
                    updated.user_id, updated.reserved_cost
                )
            expired_count += 1
            logger.info(
                "求片超时自动取消并释放积分: request_id={}, user_id={}, released_cost={}",
                updated.id,
                updated.user_id,
                updated.reserved_cost,
            )
        return expired_count

    async def get_user_requests(
        self, user_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """获取用户的求片申请及积分预留/结算/释放追踪列表"""
        items = await self.request_repo.get_by_user(user_id, limit=limit)
        return [
            {
                "id": item.id,
                "user_id": item.user_id,
                "media_server_id": item.media_server_id,
                "arr_id": item.arr_id,
                "library_name": item.library_name,
                "media_type": item.media_type,
                "media_id": item.media_id,
                "title": item.title,
                "reserved_cost": item.reserved_cost,
                "status": item.status,
                "cost_status": item.cost_status,
                "approver_name": item.approver_name,
                "fail_reason": item.fail_reason,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                "resolved_at": (
                    item.resolved_at.isoformat() if item.resolved_at else None
                ),
            }
            for item in items
        ]

    async def get_request_cost(self) -> int:
        """获取求片消耗积分"""
        renew_score = await self.telegram_repo.get_renew_score()
        return int(renew_score * 0.1)

    async def get_requestable_libraries(self, media_server_id: int) -> list[dict]:
        """API: 获取可求片的库列表（按 Emby/Jellyfin 实例筛选）"""
        bindings = await self.binding_repo.get_by_media_id(media_server_id)
        valid_bindings = []
        for binding in bindings:
            client = self._sonarr_clients.get(
                binding.arr_id
            ) or self._radarr_clients.get(binding.arr_id)
            if client:
                type_ = "sonarr" if isinstance(client, SonarrClient) else "radarr"
                valid_bindings.append({"name": binding.library_name, "type": type_})
        return valid_bindings

    async def search_media_items(
        self, media_server_id: int, library_name: str, query: str
    ) -> list[dict]:
        """API: 搜索媒体"""
        client, _ = await self._get_client_by_library(media_server_id, library_name)
        if not client:
            return []

        results = []
        try:
            async for item in client.lookup(query):
                results.append(item)
                if len(results) >= 20:  # Limit for API
                    break
        except Exception as e:
            logger.error("API Search failed: {}", e)
            return []

        data = []
        for item in results:
            media_id = getattr(item, "tvdbId", getattr(item, "tmdbId", 0))
            poster = self._extract_poster(item)
            year = getattr(item, "year", 0)
            status = "new"

            item_id = getattr(item, "id", None)

            if isinstance(item_id, int) and item_id > 0:
                status = "existing"

            data.append(
                {
                    "media_id": media_id,
                    "title": item.title,
                    "year": year,
                    "poster": poster,
                    "status": status,
                    "overview": getattr(item, "overview", ""),
                }
            )
        return data

    async def submit_request_api(
        self, user_id: int, media_server_id: int, library_name: str, media_id: int
    ) -> Result:
        """API: 提交求片"""
        cost = await self.get_request_cost()
        return await self.submit_final_request(
            user_id, media_server_id, library_name, media_id, cost
        )
