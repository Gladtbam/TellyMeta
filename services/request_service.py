import contextlib
import inspect
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI
from httpx2 import HTTPError
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, errors

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.economy_policy import (
    calculate_request_points_cost,
    evaluate_request_access,
)
from core.telegram_manager import TelethonClientWarper
from core.telegram_message import TelegramMessage
from core.time_utils import to_utc, utc_now
from models.events import NotificationEvent, NotificationRouteCategory
from models.orm import LibraryBinding, MediaRequest, ServerInstance
from models.radarr import MovieResource
from models.sonarr import SeriesResource
from models.tvdb import TvdbData
from repositories.binding_repo import BindingRepository
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.request_repo import RequestRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.notification_renderer import (
    EffectiveEconomyContext,
    NotificationPayload,
    RequestApprovedPayload,
    RequestArrFailedPayload,
    RequestExpiredPayload,
    RequestRejectedPayload,
    RequestSubmitPayload,
    render_request_topic_card_status,
)
from services.notification_service import NotificationService, resolve_tg_display_name
from services.user_service import Result

settings = get_settings()


async def sync_request_topic_card_status(
    notification_service: Any,
    req: MediaRequest,
    *,
    stage: Literal[
        "approved", "rejected", "failed", "expired", "canceled", "completed"
    ],
    server_name: str,
    media_title: str,
    economy: EffectiveEconomyContext,
    session: AsyncSession | None = None,
    user_name: str | None = None,
    approver_name: str | None = None,
    detail_text: str | None = None,
) -> None:
    """若求片存在 ``topic_message_id``，非致命回写群内原审核卡片并清空过期按钮。

    Args:
        notification_service: 统一通知服务实例。
        req: 求片 ORM 记录。
        stage: 终结或入库阶段标识。
        server_name: 目标媒体服务器名称。
        media_title: 展示用的媒体标题。
        economy: ADR-0004 有效经济开关快照。
        session: 可选异步数据库会话。
        user_name: 可选申请人展示名。
        approver_name: 可选审核管理员展示名。
        detail_text: 可选失败/拒绝/超时详情说明。
    """
    if not req.topic_message_id:
        return
    edit_fn = getattr(notification_service, "edit_route_message", None)
    if not callable(edit_fn):
        return
    card_html = render_request_topic_card_status(
        stage=stage,
        request_id=req.id,
        user_id=req.user_id,
        server_name=server_name,
        media_title=media_title,
        media_type=req.media_type,
        media_id=req.media_id,
        reserved_cost=req.reserved_cost,
        approver_name=approver_name,
        detail_text=detail_text,
        user_name=user_name,
        economy=economy,
    )
    with contextlib.suppress(errors.RPCError, ValueError, TypeError, OSError):
        res = edit_fn(
            category=NotificationRouteCategory.MEDIA_REQUEST,
            server_id=req.arr_id,
            message_id=req.topic_message_id,
            new_content=card_html,
            clear_buttons=True,
            parse_mode="html",
            session=session,
        )
        if inspect.isawaitable(res):
            await res


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
        except (HTTPError, ValueError, KeyError, TypeError) as e:
            logger.debug(t"元数据增强失败，降级使用原始数据: {e}")

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

        # TMDB 优先级高于 TVDB：优先使用 TMDB 补充剧集标题与简介
        if tmdb_id and self.tmdb_client:
            try:
                tmdb_info = await self.tmdb_client.get_tv_series_details(tmdb_id)
                if tmdb_info:
                    if tmdb_info.name:
                        title = tmdb_info.name
                    if tmdb_info.overview:
                        overview = tmdb_info.overview
            except (HTTPError, ValueError, TypeError) as e:
                logger.debug(t"TMDB TV 查找失败 ({tmdb_id}): {e}")

        # 仅当 TMDB 未命中或缺少字段时回退到 TVDB
        if (title == default_title or not overview) and tvdb_id and self.tvdb_client:
            try:
                tvdb_resp = await self.tvdb_client.series_translations(
                    tvdb_id, language="zho"
                )
                if tvdb_resp and isinstance(tvdb_resp.data, TvdbData):
                    if title == default_title and tvdb_resp.data.name:
                        title = tvdb_resp.data.name
                    if not overview and tvdb_resp.data.overview:
                        overview = tvdb_resp.data.overview
            except (HTTPError, ValueError, TypeError) as e:
                logger.debug(t"TVDB 查找失败 ({tvdb_id}): {e}")

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
                logger.debug(t"TMDB Movie 查找失败 ({tmdb_id}): {e}")

        return title, overview

    async def submit_final_request(
        self,
        user_id: int,
        media_server_id: int,
        library_name: str,
        media_id: int,
        request_cost: int,
    ) -> Result:
        """提交求片申请：开关校验 -> 去重校验 -> 锁定并预留积分 -> 发送管理员通知（失败则回滚释放）。

        Args:
            user_id: 申请人 Telegram 用户 ID。
            media_server_id: 目标媒体服务器 ID。
            library_name: 媒体库名称。
            media_id: TMDB 或 TVDB 媒体 ID。
            request_cost: 提交时计算的固定预留积分数量。

        Returns:
            包含提交状态与提示文案的 ``Result`` 对象；未放行积分求片或校验失败时返回 ``success=False``。

        副作用：原子预留用户积分、创建求片记录并通知管理审核群组/话题。
        """
        # 查询服务器开关、定价与积分门槛：免费求片（cost == 0）完全独立于积分开关；付费求片受总开关与子开关管控
        media_server = await self.server_repo.get_by_id(media_server_id, fresh=True)
        fixed_cost = max(0, int(request_cost))
        points_allowed, points_reason = (True, "")
        if fixed_cost > 0:
            points_allowed, points_reason = await self.config_repo.check_points_feature(
                ConfigRepository.KEY_ENABLE_POINTS_REQUEST
            )
        req_ok, _, req_err_msg = evaluate_request_access(
            media_server,
            cost=fixed_cost,
            points_request_allowed=points_allowed,
            points_request_reason=points_reason,
        )
        if not req_ok:
            return Result(False, req_err_msg)

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

        # 使用绑定的 arr_id 检查求片审核路由目标是否就绪（兼容 NotificationRoute 与遗留 request_notify_topic_id）
        arr_server = await self.server_repo.get_by_id(arr_id)
        if not arr_server:
            return Result(False, "关联的 Arr 服务器实例不存在。")

        has_route = await self.notification_service.has_route_targets(
            NotificationEvent.REQUEST_SUBMIT,
            server_id=arr_id,
            session=self.request_repo.session,
        )
        if not has_route:
            return Result(
                False,
                TelegramMessage.render_markdown(
                    t"管理员未设置服务器 **{arr_server.name}** 的通知，无法提交请求。"
                ),
            )

        title, overview, poster = await self._get_media_content(selected_media, client)
        media_server_name = getattr(media_server, "name", "未知")
        raw_user_name = await self.client.get_user_name(user_id)
        user_name = (
            raw_user_name
            if isinstance(raw_user_name, str) and raw_user_name
            else str(user_id)
        )
        economy = EffectiveEconomyContext.from_cache(media_server)

        # 仅当单服求片定价 > 0 时检查并原子预留积分；0 分免费求片完全不触碰积分余额
        if fixed_cost > 0 and not await self.telegram_repo.deduct_score_if_sufficient(
            user_id, fixed_cost
        ):
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
                Button.inline("✅ 批准", data=f"req_ap_{req_id}".encode()),
                Button.inline("❌ 拒绝", data=f"req_deny_{req_id}".encode()),
            ]
        ]

        media_year_raw = getattr(selected_media, "year", None)
        submit_payload = RequestSubmitPayload(
            server_name=media_server_name,
            user_id=user_id,
            user_name=user_name,
            media_title=title,
            media_year=media_year_raw if media_year_raw else None,
            media_type=prefix.upper(),
            media_id=media_id,
            overview=overview,
            request_id=req_id,
            reserved_cost=fixed_cost,
            poster_url=poster,
            buttons=buttons,
            economy=economy,
        )
        sent_msg = await self.notification_service.emit_sync(
            submit_payload,
            server_id=arr_id,
            session=self.request_repo.session,
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
            logger.warning(
                "免费求片通知发送失败: request_id={}, user_id={}",
                req_id,
                user_id,
            )
            return Result(
                False,
                "❌ 求片通知发送失败，请稍后重试。",
            )

        msg_id = getattr(sent_msg, "id", None)
        if msg_id:
            await self.request_repo.update_topic_message_id(req_id, msg_id)

        logger.info(
            "求片提交成功: request_id={}, user_id={}, arr_id={}, media={}:{}, reserved_cost={}",
            req_id,
            user_id,
            arr_id,
            prefix,
            media_id,
            fixed_cost,
        )
        if fixed_cost > 0:
            return Result(
                True,
                f"✅ 请求已成功提交！(已预留 **{fixed_cost}** 积分，请求编号 #{req_id})\n请耐心等待管理员审核。",
            )
        return Result(
            True,
            f"✅ 请求已成功提交！（请求编号 #{req_id}）\n请耐心等待管理员审核。",
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
        except (HTTPError, ValueError, KeyError, TypeError) as e:
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
                TelegramMessage.render_markdown(
                    t"✅ 已批准并添加 **{msg}** (操作人: {approver_name})"
                ),
            )
        return Result(False, msg)

    async def _resolve_media_server_and_economy(
        self, media_server_id: int
    ) -> tuple[str, EffectiveEconomyContext]:
        """读取目标媒体服务器名称与遵循 ADR-0004 的有效经济开关快照。"""
        server = await self.server_repo.get_by_id(media_server_id)
        server_name = server.name if server else f"#{media_server_id}"
        return server_name, EffectiveEconomyContext.from_cache(server)

    async def _safe_get_user_name(self, user_id: int) -> str | None:
        """非致命读取申请人展示名称，供编辑原审核卡片时显示。"""
        name = await resolve_tg_display_name(self.client, user_id)
        return name or None

    async def _sync_topic_card_status(
        self,
        req: MediaRequest,
        *,
        stage: Literal[
            "approved", "rejected", "failed", "expired", "canceled", "completed"
        ],
        server_name: str,
        economy: EffectiveEconomyContext,
        approver_name: str | None = None,
        detail_text: str | None = None,
    ) -> None:
        """若请求记录存在 topic_message_id，非致命更新群内原审核卡片文本并移除过期内联按钮。"""
        if not req.topic_message_id:
            return
        user_name = await self._safe_get_user_name(req.user_id)
        await sync_request_topic_card_status(
            self.notification_service,
            req,
            stage=stage,
            server_name=server_name,
            media_title=req.title or str(req.media_id),
            economy=economy,
            session=self.request_repo.session,
            user_name=user_name,
            approver_name=approver_name,
            detail_text=detail_text,
        )

    async def _emit_requester_dm(
        self,
        request_id: int,
        user_id: int,
        stage: str,
        payload: NotificationPayload,
        *,
        server_id: int,
    ) -> None:
        """通过统一异步管线发射求片状态变更私聊通知（失败自动兜底收件箱，不阻断主流程）。"""
        try:
            await self.notification_service.emit(
                payload,
                user_id=user_id,
                server_id=server_id,
                session=self.request_repo.session,
            )
        except (errors.RPCError, ValueError, TypeError, OSError) as exc:
            logger.warning(
                "求片结果通知入队异常（不影响已完成状态与积分）: request_id={}, user_id={}, stage={}, err={}",
                request_id,
                user_id,
                stage,
                exc,
            )

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

        now = utc_now()
        if req.expires_at <= now:
            # 已超期但定时任务尚未扫描到时，在此直接触发超时释放，禁止再批准
            default_expire_days = (await self.config_repo.get_policy_config())[
                "request_expire_days"
            ]
            req_days = (
                max(1, (req.expires_at - req.created_at).days)
                if req.expires_at and req.created_at
                else default_expire_days
            )
            expired_req = await self.request_repo.atomic_transition(
                request_id,
                from_status="pending",
                to_status="expired",
                cost_status="released",
                fail_reason=f"待审核超过{req_days}天自动取消",
                now=now,
            )
            if expired_req:
                if expired_req.reserved_cost > 0:
                    await self.telegram_repo.update_score(
                        expired_req.user_id, expired_req.reserved_cost
                    )
                server_name, economy = await self._resolve_media_server_and_economy(
                    expired_req.media_server_id
                )
                await self._sync_topic_card_status(
                    expired_req,
                    stage="expired",
                    server_name=server_name,
                    economy=economy,
                    detail_text=expired_req.fail_reason,
                )
                await self._emit_requester_dm(
                    expired_req.id,
                    expired_req.user_id,
                    "expired",
                    RequestExpiredPayload(
                        server_name=server_name,
                        media_title=expired_req.title or str(expired_req.media_id),
                        expire_days=req_days,
                        reserved_cost=expired_req.reserved_cost,
                        request_id=expired_req.id,
                        economy=economy,
                    ),
                    server_id=expired_req.media_server_id,
                )
            if expired_req and expired_req.reserved_cost > 0:
                return Result(
                    False,
                    "该求片请求已超过 7 天有效期并自动取消，已释放预留积分，无法批准。",
                )
            return Result(
                False,
                "该求片请求已超过 7 天有效期并自动取消，无法批准。",
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
            approved_req = await self.request_repo.atomic_transition(
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
            server_name, economy = await self._resolve_media_server_and_economy(
                claimed.media_server_id
            )
            target_req = approved_req or claimed
            await self._sync_topic_card_status(
                target_req,
                stage="approved",
                server_name=server_name,
                economy=economy,
                approver_name=approver_name,
            )
            await self._emit_requester_dm(
                request_id,
                claimed.user_id,
                "approved",
                RequestApprovedPayload(
                    server_name=server_name,
                    media_title=claimed.title or msg,
                    approver_name=approver_name,
                    reserved_cost=claimed.reserved_cost,
                    request_id=request_id,
                    economy=economy,
                ),
                server_id=claimed.media_server_id,
            )
            if claimed.reserved_cost > 0:
                return Result(
                    True,
                    TelegramMessage.render_markdown(
                        t"✅ 已批准并添加 **{msg}**（结算积分: {claimed.reserved_cost}，操作人: {approver_name}）"
                    ),
                )
            return Result(
                True,
                TelegramMessage.render_markdown(
                    t"✅ 已批准并添加 **{msg}**（操作人: {approver_name}）"
                ),
            )

        # Arr 添加失败时仅在曾预留积分的情况下退回
        failed_req = await self.request_repo.atomic_transition(
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
            "Arr 添加失败: request_id={}, user_id={}, released_cost={}, reason={}",
            request_id,
            claimed.user_id,
            claimed.reserved_cost,
            msg,
        )
        server_name, economy = await self._resolve_media_server_and_economy(
            claimed.media_server_id
        )
        target_req = failed_req or claimed
        await self._sync_topic_card_status(
            target_req,
            stage="failed",
            server_name=server_name,
            economy=economy,
            approver_name=approver_name,
            detail_text=msg,
        )
        await self._emit_requester_dm(
            request_id,
            claimed.user_id,
            "arr_failed",
            RequestArrFailedPayload(
                server_name=server_name,
                media_title=claimed.title or str(claimed.media_id),
                fail_reason=msg,
                approver_name=approver_name,
                reserved_cost=claimed.reserved_cost,
                request_id=request_id,
                economy=economy,
            ),
            server_id=claimed.media_server_id,
        )
        if claimed.reserved_cost > 0:
            return Result(
                False,
                TelegramMessage.render_markdown(
                    t"❌ {msg}（已释放预留积分 {claimed.reserved_cost}）"
                ),
            )
        return Result(False, TelegramMessage.render_markdown(t"❌ {msg}"))

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
        server_name, economy = await self._resolve_media_server_and_economy(
            updated.media_server_id
        )
        has_cost = updated.reserved_cost > 0
        reason_text = f"管理员（{approver_name}）已拒绝该请求。"
        await self._sync_topic_card_status(
            updated,
            stage="rejected",
            server_name=server_name,
            economy=economy,
            approver_name=approver_name,
            detail_text=updated.fail_reason,
        )
        await self._emit_requester_dm(
            request_id,
            updated.user_id,
            "rejected",
            RequestRejectedPayload(
                server_name=server_name,
                media_title=updated.title or f"#{request_id}",
                reason=reason_text,
                approver_name=approver_name,
                reserved_cost=updated.reserved_cost,
                request_id=request_id,
                economy=economy,
            ),
            server_id=updated.media_server_id,
        )
        if has_cost:
            return Result(
                True,
                TelegramMessage.render_markdown(
                    t"❌ 已拒绝求片请求 #{request_id}（操作人: {approver_name}），已释放预留积分 {updated.reserved_cost}。"
                ),
            )
        return Result(
            True,
            TelegramMessage.render_markdown(
                t"❌ 已拒绝求片请求 #{request_id}（操作人: {approver_name}）。"
            ),
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
        server_name, economy = await self._resolve_media_server_and_economy(
            updated.media_server_id
        )
        await self._sync_topic_card_status(
            updated,
            stage="canceled",
            server_name=server_name,
            economy=economy,
            detail_text="申请人主动撤销",
        )
        if updated.reserved_cost > 0:
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
        logger.info(
            "申请人已撤销免费求片: request_id={}, user_id={}",
            request_id,
            updated.user_id,
        )
        return Result(True, f"已撤销求片请求 #{request_id}。")

    async def expire_pending_requests(self, now: datetime | None = None) -> int:
        """超时清理：自动取消超过配置天数的待审核求片请求并释放全部预留积分"""
        current_time = to_utc(now) if now is not None else utc_now()
        default_expire_days = (await self.config_repo.get_policy_config())[
            "request_expire_days"
        ]
        expired_list = await self.request_repo.find_expired_pending(now=current_time)
        expired_count = 0
        for req in expired_list:
            req_days = (
                max(1, (req.expires_at - req.created_at).days)
                if req.expires_at and req.created_at
                else default_expire_days
            )
            updated = await self.request_repo.atomic_transition(
                req.id,
                from_status="pending",
                to_status="expired",
                cost_status="released",
                fail_reason=f"待审核超过{req_days}天自动取消",
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
            server_name, economy = await self._resolve_media_server_and_economy(
                updated.media_server_id
            )
            await self._sync_topic_card_status(
                updated,
                stage="expired",
                server_name=server_name,
                economy=economy,
                detail_text=updated.fail_reason,
            )
            await self._emit_requester_dm(
                updated.id,
                updated.user_id,
                "expired",
                RequestExpiredPayload(
                    server_name=server_name,
                    media_title=updated.title or str(updated.media_id),
                    expire_days=req_days,
                    reserved_cost=updated.reserved_cost,
                    request_id=updated.id,
                    economy=economy,
                ),
                server_id=updated.media_server_id,
            )
        return expired_count

    async def get_user_requests(
        self, user_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """获取用户的求片申请及预留/结算状态列表；0 分免费求片隐藏积分标签。"""
        cost_labels = {
            "reserved": "已预留",
            "settled": "已结算",
            "released": "已释放",
        }
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
                "is_free": int(item.reserved_cost or 0) <= 0,
                "cost_label": (
                    ""
                    if int(item.reserved_cost or 0) <= 0
                    else f"积分：{item.reserved_cost} ({cost_labels.get(item.cost_status, item.cost_status)})"
                ),
                "status": item.status,
                "cost_status": item.cost_status,
                "approver_name": item.approver_name,
                "fail_reason": item.fail_reason,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                "resolved_at": (
                    item.resolved_at.isoformat() if item.resolved_at else None
                ),
                "completed_at": (
                    item.completed_at.isoformat() if item.completed_at else None
                ),
            }
            for item in items
        ]

    async def get_request_cost(
        self,
        server_id: int | None = None,
        *,
        server: ServerInstance | None = None,
    ) -> int:
        """计算指定服务器单次求片消耗积分（支持 fixed 与 ratio，允许返回 0）。

        Args:
            server_id: 目标媒体服务器 ID。
            server: 可选已加载的服务器实例（避免重复查询）。

        Returns:
            非负整数消耗积分数；0 表示免费求片。
        """
        target = server
        if target is None and server_id is not None:
            target = await self.server_repo.get_by_id(server_id, fresh=True)
        if target is None:
            return 0
        renew_score = 0
        if target.request_points_mode != "fixed":
            renew_score = await self.telegram_repo.get_renew_score()
        return calculate_request_points_cost(target, renew_score)

    async def resolve_request_cost_and_access(
        self, server_id: int
    ) -> tuple[ServerInstance | None, int, bool, str, str]:
        """解析目标服务器的求片定价与路由放行状态，确保选库、搜索与提交门禁完全一致。

        Args:
            server_id: 目标媒体服务器 ID。

        Returns:
            ``(server, cost, allowed, error_code, error_message)``。
        """
        server = await self.server_repo.get_by_id(server_id, fresh=True)
        if server is None:
            ok, code, msg = evaluate_request_access(
                None,
                cost=0,
                points_request_allowed=False,
                points_request_reason="",
            )
            return None, 0, ok, code, msg

        cost = await self.get_request_cost(server=server)
        points_allowed, points_reason = (True, "")
        if cost > 0:
            points_allowed, points_reason = await self.config_repo.check_points_feature(
                ConfigRepository.KEY_ENABLE_POINTS_REQUEST
            )
        ok, code, msg = evaluate_request_access(
            server,
            cost=cost,
            points_request_allowed=points_allowed,
            points_request_reason=points_reason,
        )
        return server, cost, ok, code, msg

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
        except (HTTPError, ValueError, KeyError, TypeError) as e:
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
        cost = await self.get_request_cost(media_server_id)
        return await self.submit_final_request(
            user_id, media_server_id, library_name, media_id, cost
        )
