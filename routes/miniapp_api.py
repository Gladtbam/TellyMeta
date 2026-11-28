import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.media import run_subtitle_upload_flow
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.database import get_db
from core.dependencies import (
    get_radarr_clients,
    get_sonarr_clients,
    get_telethon_client,
)
from core.errors import ApiError
from core.task_utils import create_background_task
from core.telegram_manager import TelethonClientWarper
from core.time_utils import to_utc
from core.webapp_auth import (
    get_current_user_id,
    require_group_member,
    require_owned_media_account,
    validate_admin_access,
)
from models.orm import RegistrationMode
from models.schemas import (
    AdminActionResponse,
    AdminTrafficAdjustDto,
    AutoRenewToggleDto,
    AvailableServerDto,
    ChangeScoreDto,
    MediaAccountDto,
    MediaItemDto,
    MiniAppNotificationsResponse,
    NotificationInboxItemDto,
    NotificationMarkReadDto,
    NotificationMarkReadResponse,
    OpsAlertLogsPageResponse,
    RequestLibraryDto,
    RequestSubmitDto,
    ToggleResponse,
    TrafficPackRedeemDto,
    TrafficPackRedeemResponse,
    UserInfoDto,
    UserNotificationPreferencesDto,
    UserNotificationPreferencesResponse,
    UserNotificationPreferencesUpdateDto,
)
from repositories.config_repo import DEFAULT_TRAFFIC_PACK_UNIT_BYTES
from repositories.notification_repo import (
    NotificationInboxRepository,
    UserNotificationPreferenceRepository,
)
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from routes.settings_api import query_recent_ops_logs_page
from services.account_service import AccountService
from services.notification_renderer import SelfActionReceiptPayload
from services.notification_service import (
    NotificationService,
    get_or_create_notification_runtime,
)
from services.request_service import RequestService
from services.user_service import UserService

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])

_IDEMPOTENCY_TTL_SECONDS = 300.0


def _get_idempotency_state(
    app: Any,
) -> tuple[
    dict[tuple[int, str, str, str], tuple[float, dict[str, Any]]],
    dict[tuple[int, str, str], asyncio.Lock],
]:
    """获取挂载在 app.state 上的幂等缓存与操作互斥锁。"""
    cache = getattr(app.state, "_miniapp_idempotency_cache", None)
    if cache is None:
        cache = {}
        app.state._miniapp_idempotency_cache = cache

    locks = getattr(app.state, "_miniapp_action_locks", None)
    if locks is None:
        locks = {}
        app.state._miniapp_action_locks = locks

    return cache, locks


def _format_plain_sensitive_text(markdown_text: str) -> str:
    """将 Markdown 格式的凭据/激活码消息整理为适合 Mini App 弹窗展示的纯文本。"""
    return markdown_text.replace("`", "").replace("**", "").strip()


def _build_sensitive_fallback_payload(action: str) -> SelfActionReceiptPayload:
    """构造敏感操作脱敏站内兜底载荷（绝不携带明文密码或激活码）。"""
    if action == "signup":
        return SelfActionReceiptPayload(
            action_type="register",
            summary="媒体账号已注册（系统不留存明文密码）。",
        )
    if action == "reset_password":
        return SelfActionReceiptPayload(
            action_type="reset_password",
            summary="媒体账号密码已重置（系统不留存明文密码）。",
        )
    if action == "redeem_code":
        return SelfActionReceiptPayload(
            action_type="redeem_code",
            summary="激活码已核销（系统不留存明文凭据）。",
        )
    return SelfActionReceiptPayload(
        action_type="other",
        summary="安全敏感操作已完成（系统不留存明文凭据）。",
    )


async def _deliver_sensitive_result(
    client: TelethonClientWarper,
    user_id: int,
    action: str,
    raw_message: str,
    dm_success_notice: str,
    dm_failed_prefix: str,
    *,
    app: Any | None = None,
    session: AsyncSession | None = None,
    server_id: int | None = None,
    skip_inbox_fallback: bool = True,
) -> dict[str, Any]:
    """优先通过统一通知管线发送私聊敏感结果；失败时降级为 Mini App 本人一次性展示。

    Note:
        由于请求发起方已在 Mini App 当前会话内等待响应，私聊失败时会当场回传
        ``one_time_result`` 前端一次性弹窗，因此显式传 ``skip_inbox_fallback=True``
        给 ``NotificationService.deliver_direct_dm``：绝不写入 ``NotificationInboxItem``，
        避免站内重复提醒与明文凭据落库。
    """
    target_app = (
        app
        if app is not None
        else SimpleNamespace(state=SimpleNamespace(telethon_client=client))
    )
    dm_delivered, err_detail = await NotificationService(target_app).deliver_direct_dm(
        user_id=user_id,
        message=raw_message,
        parse_mode="markdown",
        client=client,
        skip_inbox_fallback=skip_inbox_fallback,
        fallback_payload=_build_sensitive_fallback_payload(action),
        server_id=server_id,
        session=session,
    )
    if not dm_delivered:
        logger.warning(
            "Telegram 私聊交付敏感结果失败，降级为前端一次性展示（skip_inbox_fallback={}）: user_id={}, action={}, err={}",
            skip_inbox_fallback,
            user_id,
            action,
            err_detail,
        )

    if dm_delivered:
        return {
            "success": True,
            "dm_delivered": True,
            "one_time_result": None,
            "message": dm_success_notice,
        }

    plain_result = _format_plain_sensitive_text(raw_message)
    return {
        "success": True,
        "dm_delivered": False,
        "one_time_result": plain_result,
        "message": (
            f"{dm_failed_prefix}，但 Telegram 私聊未送达，"
            f"请立即保存以下信息（仅本次展示）：\n\n{plain_result}"
        ),
    }


async def _run_idempotent_action(
    request: Request,
    *,
    user_id: int,
    action: str,
    lock_target: str,
    cache_target: str,
    idempotency_key: str | None,
    executor: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """执行带用户维度互斥锁与短期结果缓存的幂等操作，防止网络重传导致重复扣分或重复下发

    Args:
        request: FastAPI 请求对象，用于在 ``app.state`` 上存储并发锁与幂等结果缓存。
        user_id: 当前操作的 Telegram 用户 ID。
        action: 操作标识名称。
        lock_target: 并发互斥锁粒度标识（例如服务器 ID 或操作目标）。
        cache_target: 幂等缓存键粒度标识（可附带数量等请求参数）。
        idempotency_key: 客户端传入的 ``X-Idempotency-Key``；为空时不读写缓存仅做串行互斥。
        executor: 实际业务执行协程，返回接口响应字典（仅 ``success`` 为真时写入缓存）。

    Returns:
        命中短期幂等缓存时返回缓存副本，否则返回本次 ``executor()`` 的执行结果。
    """
    cache, locks = _get_idempotency_state(request.app)
    lock_key = (user_id, action, lock_target)
    lock = locks.setdefault(lock_key, asyncio.Lock())
    clean_key = idempotency_key.strip() if idempotency_key else ""

    async with lock:
        now = time.monotonic()
        # 清理过期的幂等缓存条目
        expired_keys = [k for k, (exp, _) in cache.items() if exp <= now]
        for k in expired_keys:
            cache.pop(k, None)

        cache_key = (user_id, action, cache_target, clean_key)
        if clean_key and cache_key in cache:
            _, cached_resp = cache[cache_key]
            return dict(cached_resp)

        response = await executor()
        if clean_key and response.get("success"):
            cache[cache_key] = (now + _IDEMPOTENCY_TTL_SECONDS, dict(response))
        return response


async def _execute_idempotent_sensitive_action(
    request: Request,
    user_id: int,
    action: str,
    target_key: str,
    idempotency_key: str | None,
    client: TelethonClientWarper,
    operation: Callable[[], Awaitable[Any]],
    dm_success_notice: str,
    dm_failed_prefix: str,
    *,
    session: AsyncSession | None = None,
    server_id: int | None = None,
) -> dict[str, Any]:
    """执行带并发互斥与网络重试幂等保护的敏感操作。"""

    async def _run() -> dict[str, Any]:
        result = await operation()
        if not result.success:
            return {"success": False, "message": result.message}
        return await _deliver_sensitive_result(
            client=client,
            user_id=user_id,
            action=action,
            raw_message=result.message,
            dm_success_notice=dm_success_notice,
            dm_failed_prefix=dm_failed_prefix,
            app=request.app,
            session=session,
            server_id=server_id,
            skip_inbox_fallback=True,
        )

    return await _run_idempotent_action(
        request,
        user_id=user_id,
        action=action,
        lock_target=target_key,
        cache_target=target_key,
        idempotency_key=idempotency_key,
        executor=_run,
    )


def _build_user_info_dto(
    data: dict[str, Any],
    renew_score: int,
    *,
    is_admin: bool,
    is_group_member: bool,
    has_username: bool,
    username: str | None = None,
    full_name: str | None = None,
    unread_notification_count: int = 0,
    has_unread_mandatory_notification: bool = False,
) -> UserInfoDto:
    """将 ``UserService.get_user_info_data`` 结果统一转换为 ``UserInfoDto``。

    Args:
        data: 用户聚合字典（含账户列表及周期/总播放时长汇总）。
        renew_score: 全局统一月续期基础积分。
        is_admin: 是否为管理员。
        is_group_member: 是否仍在官方群组内。
        has_username: 是否配置了 Telegram 用户名。
        username: 可选用户名（管理员查询视角使用）。
        full_name: 可选显示名称（管理员查询视角使用）。

    Returns:
        可直接序列化的 ``UserInfoDto`` 实例。
    """
    user = data["user"]
    media_accounts_data = data["media_accounts"]
    available_servers_data = data.get("available_servers", [])

    return UserInfoDto(
        id=user.id,
        username=username,
        full_name=full_name,
        score=user.score,
        checkin_count=user.checkin_count,
        consecutive_checkin_count=user.current_consecutive_checkin_count,
        warning_count=user.warning_count,
        renew_score=int(renew_score),
        is_admin=is_admin,
        is_group_member=is_group_member,
        has_username=has_username,
        cycle_watch_time_seconds=int(data.get("cycle_watch_time_seconds", 0)),
        total_watch_time_seconds=int(data.get("total_watch_time_seconds", 0)),
        enable_points=bool(data.get("enable_points", True)),
        enable_points_checkin=bool(data.get("enable_points_checkin", True)),
        enable_points_chat=bool(data.get("enable_points_chat", True)),
        enable_points_register=bool(data.get("enable_points_register", True)),
        enable_points_renew=bool(data.get("enable_points_renew", True)),
        enable_points_request=bool(data.get("enable_points_request", True)),
        enable_points_generate_code=bool(data.get("enable_points_generate_code", True)),
        enable_points_traffic_pack=bool(data.get("enable_points_traffic_pack", True)),
        enable_points_spam=bool(data.get("enable_points_spam", True)),
        enable_points_warn=bool(data.get("enable_points_warn", True)),
        enable_watch_time=bool(data.get("enable_watch_time", True)),
        enable_traffic=bool(data.get("enable_traffic", False)),
        unread_notification_count=int(unread_notification_count),
        has_unread_mandatory_notification=bool(has_unread_mandatory_notification),
        media_accounts=[
            MediaAccountDto(
                server_id=item["media_user"].server_id,
                media_name=item["media_name"],
                server_name=item["server_name"],
                server_type=item["server_type"],
                server_url=item["server_url"],
                status_text=item["status_text"],
                expires_at=item["expires_at"],
                is_banned=item["is_banned"],
                allow_subtitle_upload=bool(item.get("allow_subtitle_upload", False)),
                allow_request=bool(item.get("allow_request", False)),
                request_open=bool(item.get("request_open", False)),
                request_points_mode=str(item.get("request_points_mode") or "fixed"),
                request_points_cost=int(item.get("request_points_cost", 0)),
                tos=item.get("tos"),
                enable_watch_time=bool(item.get("enable_watch_time", True)),
                cycle_watch_time_seconds=int(item.get("cycle_watch_time_seconds", 0)),
                total_watch_time_seconds=int(item.get("total_watch_time_seconds", 0)),
                anchor_day=item.get("anchor_day"),
                next_reset_at=item.get("next_reset_at"),
                auto_renew=bool(item.get("auto_renew", False)),
                auto_renew_open=bool(item.get("auto_renew_open", False)),
                enable_watch_time_renewal=bool(
                    item.get("enable_watch_time_renewal", False)
                ),
                watch_time_renewal_threshold_seconds=int(
                    item.get("watch_time_renewal_threshold_seconds", 0)
                ),
                watch_time_renewal_open=bool(
                    item.get("watch_time_renewal_open", False)
                ),
                watch_time_renewal_eligible=bool(
                    item.get("watch_time_renewal_eligible", False)
                ),
                watch_time_renewal_status_text=str(
                    item.get("watch_time_renewal_status_text")
                    or "当前服务器未开放保活续期"
                ),
                watch_time_progress_percent=item.get("watch_time_progress_percent"),
                enable_traffic=bool(item.get("enable_traffic", False)),
                traffic_limited=bool(item.get("traffic_limited", False)),
                cycle_traffic_bytes=int(item.get("cycle_traffic_bytes", 0)),
                traffic_pack_bytes=int(item.get("traffic_pack_bytes", 0)),
                total_traffic_bytes=int(item.get("total_traffic_bytes", 0)),
                server_traffic_quota_bytes=int(
                    item.get("server_traffic_quota_bytes", 0)
                ),
                cycle_traffic_adjustment_bytes=int(
                    item.get("cycle_traffic_adjustment_bytes", 0)
                ),
                traffic_quota_bytes=int(item.get("traffic_quota_bytes", 0)),
                traffic_remaining_bytes=item.get("traffic_remaining_bytes"),
                traffic_total_remaining_bytes=item.get("traffic_total_remaining_bytes"),
                is_traffic_suspended=bool(item.get("is_traffic_suspended", False)),
                enable_traffic_auto_suspend=bool(
                    item.get("enable_traffic_auto_suspend", False)
                ),
                allow_traffic_pack=bool(item.get("allow_traffic_pack", False)),
                traffic_pack_open=bool(item.get("traffic_pack_open", False)),
                traffic_pack_points_mode=str(
                    item.get("traffic_pack_points_mode") or "fixed"
                ),
                traffic_pack_cost_score=int(item.get("traffic_pack_cost_score", 50)),
                traffic_pack_size_gib=float(item.get("traffic_pack_size_gib", 10.0)),
                traffic_pack_unit_bytes=int(
                    item.get("traffic_pack_unit_bytes", DEFAULT_TRAFFIC_PACK_UNIT_BYTES)
                ),
                can_points_renew=bool(item.get("can_points_renew", True)),
                can_generate_code=bool(item.get("can_generate_code", True)),
            )
            for item in media_accounts_data
        ],
        available_servers=[
            AvailableServerDto(**item) for item in available_servers_data
        ],
    )


@router.get("/me", response_model=UserInfoDto)
async def get_my_info(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> UserInfoDto:
    """获取当前 MiniApp 用户信息"""
    service = UserService(request.app, session)
    telegram_repo = TelegramRepository(session)
    try:
        data = await service.get_user_info_data(user_id)
        renew_score = await telegram_repo.get_renew_score()

        user = data["user"]
        is_admin = user.id in getattr(request.app.state, "admin_ids", set())

        # 检查用户是否在群组中
        is_group_member = False
        has_username = False

        with contextlib.suppress(Exception):
            participant = await client.get_participant(user_id)
            is_group_member = participant is not None

        with contextlib.suppress(Exception):
            uname = await client.get_user_name(user_id, need_username=True)
            has_username = uname not in (None, False)

        now = to_utc(get_or_create_notification_runtime(request.app).clock())
        unread_count, has_mandatory = await NotificationInboxRepository(
            session
        ).get_unread_summary(user_id, since=now - timedelta(days=30))

        return _build_user_info_dto(
            data,
            int(renew_score),
            is_admin=is_admin,
            is_group_member=is_group_member,
            has_username=has_username,
            unread_notification_count=unread_count,
            has_unread_mandatory_notification=has_mandatory,
        )

    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/notifications", response_model=MiniAppNotificationsResponse)
async def get_my_notifications(
    request: Request,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> MiniAppNotificationsResponse:
    """获取当前成员近 30 天站内兜底收件箱、未读汇总、个人提醒偏好及管理员运维告警。"""
    now = to_utc(get_or_create_notification_runtime(request.app).clock())
    since = now - timedelta(days=30)

    inbox_repo = NotificationInboxRepository(session)
    pref_repo = UserNotificationPreferenceRepository(session)
    items = await inbox_repo.list_user_items(
        user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
        since=since,
    )
    unread_count, has_mandatory = await inbox_repo.get_unread_summary(
        user_id, since=since
    )
    pref_flags = await pref_repo.get_effective_flags(user_id)

    servers = await ServerRepository(session).get_all()
    server_names = {srv.id: srv.name for srv in servers}
    item_dtos = [
        NotificationInboxItemDto(
            id=item.id,
            server_id=item.server_id,
            server_name=(
                server_names.get(item.server_id) if item.server_id is not None else None
            ),
            event_type=str(item.event_type),
            category=str(item.category),
            severity=str(item.severity),
            title=item.title,
            body_text=item.body_text,
            action_url=item.action_url,
            is_mandatory=bool(item.is_mandatory),
            is_read=bool(item.is_read),
            delivery_error=item.delivery_error,
            created_at=item.created_at,
            read_at=item.read_at,
        )
        for item in items
    ]

    admin_ids = getattr(request.app.state, "admin_ids", set())
    is_admin = user_id in admin_ids
    ops_logs = (
        (await query_recent_ops_logs_page(session, now=now, limit=50, offset=0)).items
        if is_admin
        else []
    )

    return MiniAppNotificationsResponse(
        items=item_dtos,
        unread_count=unread_count,
        has_unread_mandatory=has_mandatory,
        preferences=UserNotificationPreferencesDto(**pref_flags),
        is_admin=is_admin,
        ops_logs=ops_logs,
    )


@router.get(
    "/notifications/ops_logs",
    dependencies=[Depends(validate_admin_access)],
    response_model=OpsAlertLogsPageResponse,
)
async def get_miniapp_ops_logs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    server_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
) -> OpsAlertLogsPageResponse:
    """管理员在 Mini App 内分页查询近 30 天运维告警记录。"""
    now = to_utc(get_or_create_notification_runtime(request.app).clock())
    return await query_recent_ops_logs_page(
        session,
        now=now,
        limit=limit,
        offset=offset,
        event_type=event_type,
        severity=severity,
        server_id=server_id,
    )


@router.post("/notifications/read", response_model=NotificationMarkReadResponse)
async def mark_my_notifications_read(
    request: Request,
    body: NotificationMarkReadDto,
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> NotificationMarkReadResponse:
    """将当前用户的单条或全部未读站内兜底通知标记为已读（严格限定本人作用域）。"""
    now = to_utc(get_or_create_notification_runtime(request.app).clock())
    since = now - timedelta(days=30)
    inbox_repo = NotificationInboxRepository(session)
    marked_count = await inbox_repo.mark_read(
        user_id,
        item_id=None if body.mark_all else body.item_id,
        now=now,
        since=since,
    )
    unread_count, has_mandatory = await inbox_repo.get_unread_summary(
        user_id, since=since
    )
    return NotificationMarkReadResponse(
        success=True,
        marked_count=marked_count,
        unread_count=unread_count,
        has_unread_mandatory=has_mandatory,
    )


@router.get(
    "/notifications/preferences",
    response_model=UserNotificationPreferencesResponse,
)
async def get_my_notification_preferences(
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> UserNotificationPreferencesResponse:
    """获取当前用户的 5 项可选通知提醒偏好设置。"""
    flags = await UserNotificationPreferenceRepository(session).get_effective_flags(
        user_id
    )
    return UserNotificationPreferencesResponse(
        success=True,
        mandatory_notice_locked=True,
        **flags,
    )


@router.put(
    "/notifications/preferences",
    response_model=UserNotificationPreferencesResponse,
)
async def update_my_notification_preferences(
    request: Request,
    body: UserNotificationPreferencesUpdateDto,
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> UserNotificationPreferencesResponse:
    """更新当前用户的可选通知提醒偏好设置（不影响必达通知投递）。"""
    now = to_utc(get_or_create_notification_runtime(request.app).clock())
    # 防止首次直调偏好接口时因尚未访问 /me 而缺少 TelegramUser 外键记录
    await TelegramRepository(session).get_or_create(user_id)
    updated = await UserNotificationPreferenceRepository(session).update_preferences(
        user_id,
        allow_watch_time_milestone=body.allow_watch_time_milestone,
        allow_pre_expiry_reminder=body.allow_pre_expiry_reminder,
        allow_traffic_80_alert=body.allow_traffic_80_alert,
        allow_cycle_reset=body.allow_cycle_reset,
        allow_self_action_receipt=body.allow_self_action_receipt,
        now=now,
    )
    return UserNotificationPreferencesResponse(
        success=True,
        allow_watch_time_milestone=bool(updated.allow_watch_time_milestone),
        allow_pre_expiry_reminder=bool(updated.allow_pre_expiry_reminder),
        allow_traffic_80_alert=bool(updated.allow_traffic_80_alert),
        allow_cycle_reset=bool(updated.allow_cycle_reset),
        allow_self_action_receipt=bool(updated.allow_self_action_receipt),
        mandatory_notice_locked=True,
    )


@router.get(
    "/admin/user/{target_user_id}",
    response_model=UserInfoDto,
    dependencies=[Depends(validate_admin_access)],
)
async def get_target_user_info_for_admin(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> UserInfoDto:
    """管理员获取指定用户信息"""
    service = UserService(request.app, session)
    telegram_repo = TelegramRepository(session)
    try:
        data = await service.get_user_info_data(target_user_id)
        renew_score = await telegram_repo.get_renew_score()

        user = data["user"]
        is_admin = user.id in getattr(request.app.state, "admin_ids", set())

        # 检查用户是否在群组中及获取用户名和昵称
        is_group_member = False
        uname = None
        first_name = None

        with contextlib.suppress(Exception):
            participant = await client.get_participant(target_user_id)
            is_group_member = participant is not None

        with contextlib.suppress(Exception):
            u_val = await client.get_user_name(target_user_id, need_username=True)
            if u_val:
                uname = str(u_val)

        with contextlib.suppress(Exception):
            fn_val = await client.get_user_name(target_user_id, need_username=False)
            if fn_val:
                first_name = str(fn_val)

        return _build_user_info_dto(
            data,
            int(renew_score),
            is_admin=is_admin,
            is_group_member=is_group_member,
            has_username=uname is not None,
            username=uname,
            full_name=first_name,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("获取目标用户信息失败: {}", e)
        raise HTTPException(status_code=500, detail="获取目标用户信息失败") from e


async def _resolve_api_operator_name(user_service: UserService, admin_id: Any) -> str:
    """安全解析管理 API 调用者展示名（兼容直接调用与 FastAPI DI）。"""
    resolved_id = admin_id if isinstance(admin_id, int) else None
    return await user_service.resolve_user_display_name(resolved_id, fallback="管理员")


@router.post(
    "/admin/user/{target_user_id}/warn",
    response_model=AdminActionResponse,
)
async def admin_warn_user(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
    admin_id: int = Depends(validate_admin_access),
) -> AdminActionResponse:
    """管理员警告目标用户 (+1)"""
    try:
        user_service = UserService(request.app, session)
        operator_name = await _resolve_api_operator_name(user_service, admin_id)
        user = await user_service.warn_user(
            target_user_id, increment=1, operator_name=operator_name
        )
        return AdminActionResponse(
            success=True,
            message=f"已警告用户，当前警告次数: {user.warning_count} 次",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("警告用户失败: {}", e)
        raise HTTPException(status_code=500, detail="警告用户操作失败") from e


@router.post(
    "/admin/user/{target_user_id}/change_score",
    response_model=AdminActionResponse,
)
async def admin_change_user_score(
    request: Request,
    target_user_id: int,
    body: ChangeScoreDto,
    session: AsyncSession = Depends(get_db),
    admin_id: int = Depends(validate_admin_access),
) -> AdminActionResponse:
    """管理员修改目标用户积分"""
    try:
        user_service = UserService(request.app, session)
        operator_name = await _resolve_api_operator_name(user_service, admin_id)
        result = await user_service.change_user_score(
            target_user_id, body.score_change, operator_name=operator_name
        )
        return AdminActionResponse(success=result.success, message=result.message)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("修改积分失败: {}", e)
        raise HTTPException(status_code=500, detail="修改用户积分失败") from e


@router.post(
    "/admin/user/{target_user_id}/del",
    response_model=AdminActionResponse,
)
async def admin_delete_user_account(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
    admin_id: int = Depends(validate_admin_access),
) -> AdminActionResponse:
    """管理员删除目标用户的媒体账户"""
    try:
        user_service = UserService(request.app, session)
        operator_name = await _resolve_api_operator_name(user_service, admin_id)
        result = await user_service.delete_account(
            target_user_id,
            "both",
            operator_name=operator_name,
            emit_notification=True,
        )
        return AdminActionResponse(
            success=result.success,
            message=result.message or "媒体账户已成功删除",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("删除媒体账户失败: {}", e)
        raise HTTPException(status_code=500, detail="删除用户媒体账户失败") from e


@router.post(
    "/admin/user/{target_user_id}/kick",
    response_model=AdminActionResponse,
)
async def admin_kick_user(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
    admin_id: int = Depends(validate_admin_access),
) -> AdminActionResponse:
    """管理员将目标用户踢出群组并删除账户"""
    try:
        user_service = UserService(request.app, session)
        operator_name = await _resolve_api_operator_name(user_service, admin_id)
        result = await user_service.kick_and_cleanup_user(
            target_user_id,
            client=client,
            operator_name=operator_name,
            suppress_kick_error=True,
        )
        return AdminActionResponse(
            success=True,
            message=f"已踢出群组并清理媒体账户。{result.message}",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("踢出用户失败: {}", e)
        raise HTTPException(status_code=500, detail="踢出用户操作失败") from e


@router.post(
    "/admin/user/{target_user_id}/ban",
    response_model=AdminActionResponse,
)
async def admin_ban_user(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
    admin_id: int = Depends(validate_admin_access),
) -> AdminActionResponse:
    """管理员在群组中封禁目标用户"""
    try:
        await client.ban_user(target_user_id)
        user_service = UserService(request.app, session)
        operator_name = await _resolve_api_operator_name(user_service, admin_id)
        await user_service.notify_admin_ban_or_kick(
            action_type="ban",
            target_user_id=target_user_id,
            operator_name=operator_name,
        )
        return AdminActionResponse(
            success=True,
            message=f"已成功封禁该用户（ID: {target_user_id}）",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("封禁用户失败: {}", e)
        raise HTTPException(status_code=500, detail="封禁用户操作失败") from e


@router.post(
    "/admin/user/{target_user_id}/accounts/{server_id}/traffic_adjust",
    response_model=AdminActionResponse,
)
async def admin_adjust_account_traffic(
    request: Request,
    target_user_id: int,
    server_id: int,
    body: AdminTrafficAdjustDto,
    session: AsyncSession = Depends(get_db),
    admin_id: int = Depends(validate_admin_access),
) -> AdminActionResponse:
    """管理员调整目标媒体账号当期临时流量额度（±GiB）或清零当期已用流量"""
    user_service = UserService(request.app, session)
    operator_name = await _resolve_api_operator_name(user_service, admin_id)
    service = AccountService(request.app, session)
    result = await service.adjust_account_cycle_traffic(
        target_user_id,
        server_id,
        delta_gib=body.delta_gib,
        reset_cycle_usage=body.reset_cycle_usage,
        operator_name=operator_name,
    )
    extra = result.extra_data if isinstance(result.extra_data, dict) else {}
    return AdminActionResponse(
        success=result.success,
        message=result.message,
        effective_quota_bytes=extra.get("effective_quota_bytes"),
        cycle_traffic_bytes=extra.get("cycle_traffic_bytes"),
        cycle_traffic_adjustment_bytes=extra.get("cycle_traffic_adjustment_bytes"),
        traffic_remaining_bytes=extra.get("traffic_remaining_bytes"),
        traffic_total_remaining_bytes=extra.get("traffic_total_remaining_bytes"),
        is_traffic_suspended=extra.get("is_traffic_suspended"),
        unsuspended=bool(extra.get("unsuspended", False)),
    )


@router.post("/redeem_code", response_model=ToggleResponse)
async def redeem_code(
    request: Request,
    code: str = Query(...),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, Any]:
    """使用兑换码（注册码/续期码）"""
    username = await client.get_user_name(user_id, need_username=True)
    if not username or username is False:
        return {"success": False, "message": "请先设置 Telegram 用户名后再使用兑换码。"}

    clean_code = code.strip()
    account_service = AccountService(request.app, session)
    return await _execute_idempotent_sensitive_action(
        request=request,
        user_id=user_id,
        action="redeem_code",
        target_key=clean_code,
        idempotency_key=x_idempotency_key,
        client=client,
        # 兑换结果由 _deliver_sensitive_result 负责私聊/前端一次性弹窗，跳过重复回执与站内落箱
        operation=lambda: account_service.redeem_code(
            user_id, username, clean_code, emit_receipt=False
        ),
        dm_success_notice="兑换成功！详细信息已发送到您的 Telegram 私聊。",
        dm_failed_prefix="兑换成功",
        session=session,
    )


@router.post("/signup/{server_id}", response_model=ToggleResponse)
async def signup_account(
    request: Request,
    server_id: int,
    verification_input: str = Query(default=""),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, Any]:
    """注册账户"""
    username = await client.get_user_name(user_id, need_username=True)
    if not username or username is False:
        return {"success": False, "message": "请先设置 Telegram 用户名后再注册。"}

    account_service = AccountService(request.app, session)

    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        return {"success": False, "message": "服务器不存在"}

    async def _do_signup() -> Any:
        if server.registration_mode == RegistrationMode.EXTERNAL:
            if not verification_input:
                return SimpleNamespace(
                    success=False,
                    message="该服务器需要验证字符串，请输入后重试。",
                )

            verify_result = await account_service.verify_external_user(
                server_id, verification_input
            )
            if not verify_result.success:
                return verify_result

            return await account_service.register(
                user_id, username, server_id, skip_checks=True, emit_receipt=False
            )
        return await account_service.register(
            user_id, username, server_id, emit_receipt=False
        )

    return await _execute_idempotent_sensitive_action(
        request=request,
        user_id=user_id,
        action="signup",
        target_key=str(server_id),
        idempotency_key=x_idempotency_key,
        client=client,
        operation=_do_signup,
        dm_success_notice="注册成功！详细信息已发送到您的 Telegram 私聊。",
        dm_failed_prefix="注册成功",
        session=session,
        server_id=server_id,
    )


@router.post("/accounts/{server_id}/toggle_nsfw", response_model=ToggleResponse)
async def toggle_account_nsfw(
    request: Request,
    server_id: int,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
) -> dict[str, bool | str]:
    """切换账户 NSFW 状态"""
    service = AccountService(request.app, session)
    result = await service.toggle_nsfw_policy(user_id, server_id)
    if not result.success:
        return {"success": False, "message": result.message}
    return {"success": True, "message": result.message}


@router.post("/accounts/{server_id}/reset_password", response_model=ToggleResponse)
async def reset_account_password(
    request: Request,
    server_id: int,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, Any]:
    """重置账户密码"""
    service = AccountService(request.app, session)
    return await _execute_idempotent_sensitive_action(
        request=request,
        user_id=user_id,
        action="reset_password",
        target_key=str(server_id),
        idempotency_key=x_idempotency_key,
        client=client,
        # 新密码经 _deliver_sensitive_result 交付（失败即前端一次性弹窗），跳过重复回执与落箱
        operation=lambda: service.forget_password(
            user_id, server_id, emit_receipt=False
        ),
        dm_success_notice="密码重置成功，新密码已发送到您的 Telegram 私聊。",
        dm_failed_prefix="密码重置成功",
        session=session,
        server_id=server_id,
    )


@router.post("/accounts/{server_id}/renew", response_model=ToggleResponse)
async def renew_account(
    request: Request,
    server_id: int,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
) -> dict[str, bool | str]:
    """续期账户"""
    service = AccountService(request.app, session)
    result = await service.renew(user_id, server_id, use_score=True)
    if not result.success:
        return {"success": False, "message": result.message}
    return {"success": True, "message": result.message}


@router.post(
    "/accounts/{server_id}/redeem_traffic_pack",
    response_model=TrafficPackRedeemResponse,
)
async def redeem_account_traffic_pack(
    request: Request,
    server_id: int,
    payload: TrafficPackRedeemDto | None = Body(default=None),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """使用全局积分兑换指定媒体账号的持久化流量包，并在满足组合状态条件时自动解除超流停用

    Args:
        request: FastAPI 请求对象。
        server_id: 目标媒体服务器 ID。
        payload: 兑换份数负载（缺省兑换 1 包）。
        x_idempotency_key: 可选幂等请求键，防止网络重传导致重复扣分。
        user_id: 已校验归属权的当前 Telegram 用户 ID。
        session: 异步数据库会话。

    Returns:
        符合 ``TrafficPackRedeemResponse`` 契约的兑换结果字典。
    """
    quantity = payload.quantity if payload is not None else 1

    async def _run() -> dict[str, Any]:
        service = AccountService(request.app, session)
        result = await service.redeem_traffic_pack(
            user_id, server_id, quantity=quantity
        )
        extra = result.extra_data if isinstance(result.extra_data, dict) else {}
        return {
            "success": bool(result.success),
            "message": result.message,
            "added_bytes": int(extra.get("added_bytes", 0)),
            "cost_score": int(extra.get("cost_score", 0)),
            "remaining_score": extra.get("remaining_score"),
            "traffic_pack_bytes": int(extra.get("traffic_pack_bytes", 0)),
            "is_traffic_suspended": bool(extra.get("is_traffic_suspended", False)),
            "unsuspended": bool(extra.get("unsuspended", False)),
        }

    return await _run_idempotent_action(
        request,
        user_id=user_id,
        action="redeem_traffic_pack",
        lock_target=str(server_id),
        cache_target=f"{server_id}:{quantity}",
        idempotency_key=x_idempotency_key,
        executor=_run,
    )


@router.post("/accounts/{server_id}/toggle_auto_renew", response_model=ToggleResponse)
async def toggle_account_auto_renew(
    request: Request,
    server_id: int,
    payload: AutoRenewToggleDto | None = Body(default=None),
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """切换或设置指定媒体账号的自动续期开关

    Args:
        request: FastAPI 请求对象
        server_id: 目标媒体服务器 ID
        payload: 可选状态负载；为空时按当前状态取反
        user_id: 已校验归属权的当前 Telegram 用户 ID
        session: 异步数据库会话

    Returns:
        包含操作结果、最新 ``new_state`` 与提示消息的字典
    """
    service = AccountService(request.app, session)
    result = await service.toggle_auto_renew(
        user_id,
        server_id,
        enabled=payload.enabled if payload is not None else None,
    )
    if not result.success:
        return {"success": False, "message": result.message}
    new_state = (
        bool(result.extra_data.get("auto_renew"))
        if isinstance(result.extra_data, dict)
        else None
    )
    return {
        "success": True,
        "key": "auto_renew",
        "new_state": new_state,
        "message": result.message,
    }


@router.post("/accounts/{server_id}/generate_code", response_model=ToggleResponse)
async def generate_account_code(
    request: Request,
    server_id: int,
    code_type: str,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, Any]:
    """生成邀请码（注册码/续期码）"""
    if code_type not in ("signup", "renew"):
        return {"success": False, "message": "无效的码类型"}

    service = AccountService(request.app, session)
    return await _execute_idempotent_sensitive_action(
        request=request,
        user_id=user_id,
        action="generate_code",
        target_key=f"{server_id}:{code_type}",
        idempotency_key=x_idempotency_key,
        client=client,
        operation=lambda: service.generate_code(user_id, code_type, server_id),
        dm_success_notice="邀请码已生成，详情已发送到您的 Telegram 私聊。",
        dm_failed_prefix="邀请码生成成功",
        session=session,
        server_id=server_id,
    )


@router.delete("/accounts/{server_id}", response_model=ToggleResponse)
async def delete_account(
    request: Request,
    server_id: int,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
) -> dict[str, bool | str]:
    """删除账户绑定"""
    service = AccountService(request.app, session)
    result = await service.delete_account(user_id, server_id)
    if not result.success:
        return {"success": False, "message": result.message}
    return {"success": True, "message": result.message}


@router.post("/tools/{server_id}/upload_subtitle", response_model=ToggleResponse)
async def trigger_upload_subtitle(
    server_id: int,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
    radarr_clients: dict[int, RadarrClient] = Depends(get_radarr_clients),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
) -> dict[str, bool | str]:
    """触发上传字幕流程"""
    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)

    if not server:
        return {"success": False, "message": "服务器不存在"}

    if not server.allow_subtitle_upload:
        return {"success": False, "message": "该服务器未开放字幕上传功能"}

    if not radarr_clients and not sonarr_clients:
        return {"success": False, "message": "媒体服务器未配置"}

    from repositories.binding_repo import BindingRepository

    bindings = await BindingRepository(session).get_by_media_id(server_id)
    has_bound_client = any(
        b.arr_id in radarr_clients or b.arr_id in sonarr_clients for b in bindings
    )
    if not bindings or not has_bound_client:
        return {
            "success": False,
            "message": "该媒体服务器未绑定可用的 Sonarr/Radarr 实例，无法上传字幕",
        }

    create_background_task(
        run_subtitle_upload_flow(
            user_id, client, None, radarr_clients, sonarr_clients, server_id
        )
    )
    return {"success": True, "message": "请返回 Telegram 聊天窗口，查看上传指引。"}


# --- Request API (求片) ---


async def _require_requestable_server(
    request: Request, session: AsyncSession, server_id: int
):
    """校验目标服务器存在、已开启求片且付费求片满足积分权限，否则统一抛出 HTTP 404/403 业务异常。

    Args:
        request: FastAPI 请求对象，用于初始化 ``RequestService`` 解析单服求片定价。
        session: 异步数据库会话。
        server_id: 目标媒体服务器 ID。

    Returns:
        校验通过的目标 ``ServerInstance``。
    """
    service = RequestService(request.app, session)
    server, _, allowed, code, message = await service.resolve_request_cost_and_access(
        server_id
    )
    if not allowed:
        status_code = 404 if code == "SERVER_NOT_FOUND" else 403
        raise ApiError(status_code, code, message)
    return server


@router.get(
    "/tools/{server_id}/request/libraries", response_model=list[RequestLibraryDto]
)
async def get_request_libraries(
    request: Request,
    server_id: int,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
):
    """获取可求片的媒体库列表"""
    await _require_requestable_server(request, session, server_id)

    service = RequestService(request.app, session)
    return await service.get_requestable_libraries(server_id)


@router.get("/tools/{server_id}/request/search", response_model=list[MediaItemDto])
async def search_media(
    request: Request,
    server_id: int,
    library: str,
    query: str,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
):
    """搜索媒体"""
    await _require_requestable_server(request, session, server_id)

    service = RequestService(request.app, session)
    return await service.search_media_items(server_id, library, query)


@router.post("/tools/{server_id}/request/submit", response_model=ToggleResponse)
async def submit_request(
    request: Request,
    server_id: int,
    payload: RequestSubmitDto,
    user_id: int = Depends(require_owned_media_account),
    session: AsyncSession = Depends(get_db),
) -> dict[str, bool | str]:
    """提交求片请求"""
    await _require_requestable_server(request, session, server_id)

    service = RequestService(request.app, session)
    result = await service.submit_request_api(
        user_id, server_id, payload.library_name, payload.media_id
    )
    if not result.success:
        raise ApiError(400, "REQUEST_SUBMIT_FAILED", result.message)
    return {"success": True, "message": result.message}


@router.get("/tools/requests")
async def list_my_requests(
    request: Request,
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """查询当前成员的求片记录及积分预留/结算/释放状态"""
    service = RequestService(request.app, session)
    items = await service.get_user_requests(user_id)
    return {"success": True, "items": items}


@router.post("/tools/requests/{request_id}/cancel")
async def cancel_my_request(
    request: Request,
    request_id: int,
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """申请人撤销待审核求片并释放全部预留积分"""
    service = RequestService(request.app, session)
    result = await service.cancel_request(request_id, user_id)
    if not result.success:
        raise ApiError(400, "REQUEST_CANCEL_FAILED", result.message)
    return {"success": True, "message": result.message}
