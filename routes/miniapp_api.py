import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
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
from core.telegram_manager import TelethonClientWarper
from core.webapp_auth import (
    get_current_user_id,
    require_group_member,
    require_owned_media_account,
    validate_admin_access,
)
from models.orm import RegistrationMode
from models.schemas import (
    AdminActionResponse,
    AvailableServerDto,
    ChangeScoreDto,
    MediaAccountDto,
    MediaItemDto,
    RequestLibraryDto,
    RequestSubmitDto,
    ToggleResponse,
    UserInfoDto,
)
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.account_service import AccountService
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


async def _deliver_sensitive_result(
    client: TelethonClientWarper,
    user_id: int,
    action: str,
    raw_message: str,
    dm_success_notice: str,
    dm_failed_prefix: str,
) -> dict[str, Any]:
    """优先通过 Telegram 私聊交付敏感结果；私聊失败时降级为 Mini App 本人一次性展示。"""
    dm_delivered = True
    try:
        await client.send_message(user_id, raw_message, parse_mode="markdown")
    except Exception as e:
        dm_delivered = False
        logger.warning(
            "Telegram 私聊交付敏感结果失败，降级为前端一次性展示: user_id={}, action={}, err={}",
            user_id,
            action,
            e,
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
) -> dict[str, Any]:
    """执行带并发互斥与网络重试幂等保护的敏感操作。"""
    cache, locks = _get_idempotency_state(request.app)
    lock_key = (user_id, action, target_key)
    lock = locks.setdefault(lock_key, asyncio.Lock())
    clean_key = idempotency_key.strip() if idempotency_key else ""

    async with lock:
        now = time.monotonic()
        # 清理过期的幂等缓存条目
        expired_keys = [k for k, (exp, _) in cache.items() if exp <= now]
        for k in expired_keys:
            cache.pop(k, None)

        cache_key = (user_id, action, target_key, clean_key)
        if clean_key and cache_key in cache:
            _, cached_resp = cache[cache_key]
            return dict(cached_resp)

        result = await operation()
        if not result.success:
            return {"success": False, "message": result.message}

        response = await _deliver_sensitive_result(
            client=client,
            user_id=user_id,
            action=action,
            raw_message=result.message,
            dm_success_notice=dm_success_notice,
            dm_failed_prefix=dm_failed_prefix,
        )
        if clean_key:
            cache[cache_key] = (now + _IDEMPOTENCY_TTL_SECONDS, dict(response))
        return response


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
        media_accounts_data = data["media_accounts"]
        available_servers_data = data.get("available_servers", [])

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

        return UserInfoDto(
            id=user.id,
            score=user.score,
            checkin_count=user.checkin_count,
            consecutive_checkin_count=user.current_consecutive_checkin_count,
            warning_count=user.warning_count,
            renew_score=int(renew_score),
            is_admin=is_admin,
            is_group_member=is_group_member,
            has_username=has_username,
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
                    allow_subtitle_upload=item.get("allow_subtitle_upload"),
                    allow_request=item.get("allow_request"),
                    tos=item.get("tos"),
                )
                for item in media_accounts_data
            ],
            available_servers=[
                AvailableServerDto(**item) for item in available_servers_data
            ],
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        media_accounts_data = data["media_accounts"]
        available_servers_data = data.get("available_servers", [])

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

        return UserInfoDto(
            id=user.id,
            username=uname,
            full_name=first_name,
            score=user.score,
            checkin_count=user.checkin_count,
            consecutive_checkin_count=user.current_consecutive_checkin_count,
            warning_count=user.warning_count,
            renew_score=int(renew_score),
            is_admin=is_admin,
            is_group_member=is_group_member,
            has_username=uname is not None,
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
                    allow_subtitle_upload=item.get("allow_subtitle_upload"),
                    allow_request=item.get("allow_request"),
                    tos=item.get("tos"),
                )
                for item in media_accounts_data
            ],
            available_servers=[
                AvailableServerDto(**item) for item in available_servers_data
            ],
        )
    except Exception as e:
        logger.exception("获取目标用户信息失败: {}", e)
        raise HTTPException(status_code=500, detail="获取目标用户信息失败") from e


@router.post(
    "/admin/user/{target_user_id}/warn",
    response_model=AdminActionResponse,
    dependencies=[Depends(validate_admin_access)],
)
async def admin_warn_user(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
) -> AdminActionResponse:
    """管理员警告目标用户 (+1)"""
    try:
        user_service = UserService(request.app, session)
        user = await user_service.telegram_repo.update_warn_and_score(
            target_user_id, increment=1
        )
        return AdminActionResponse(
            success=True,
            message=f"已警告用户，当前警告次数: {user.warning_count} 次",
        )
    except Exception as e:
        logger.exception("警告用户失败: {}", e)
        raise HTTPException(status_code=500, detail="警告用户操作失败") from e


@router.post(
    "/admin/user/{target_user_id}/change_score",
    response_model=AdminActionResponse,
    dependencies=[Depends(validate_admin_access)],
)
async def admin_change_user_score(
    request: Request,
    target_user_id: int,
    body: ChangeScoreDto,
    session: AsyncSession = Depends(get_db),
) -> AdminActionResponse:
    """管理员修改目标用户积分"""
    try:
        user_service = UserService(request.app, session)
        user = await user_service.telegram_repo.update_score(
            target_user_id, body.score_change
        )
        prefix = "+" if body.score_change > 0 else ""
        return AdminActionResponse(
            success=True,
            message=f"已修改积分 ({prefix}{body.score_change})，当前积分: {user.score}",
        )
    except Exception as e:
        logger.exception("修改积分失败: {}", e)
        raise HTTPException(status_code=500, detail="修改用户积分失败") from e


@router.post(
    "/admin/user/{target_user_id}/del",
    response_model=AdminActionResponse,
    dependencies=[Depends(validate_admin_access)],
)
async def admin_delete_user_account(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
) -> AdminActionResponse:
    """管理员删除目标用户的媒体账户"""
    try:
        user_service = UserService(request.app, session)
        result = await user_service.delete_account(target_user_id, "both")
        return AdminActionResponse(
            success=result.success,
            message=result.message or "媒体账户已成功删除",
        )
    except Exception as e:
        logger.exception("删除媒体账户失败: {}", e)
        raise HTTPException(status_code=500, detail="删除用户媒体账户失败") from e


@router.post(
    "/admin/user/{target_user_id}/kick",
    response_model=AdminActionResponse,
    dependencies=[Depends(validate_admin_access)],
)
async def admin_kick_user(
    request: Request,
    target_user_id: int,
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> AdminActionResponse:
    """管理员将目标用户踢出群组并删除账户"""
    try:
        with contextlib.suppress(Exception):
            await client.kick_and_ban_participant(target_user_id)

        user_service = UserService(request.app, session)
        result = await user_service.delete_account(target_user_id, "both")
        return AdminActionResponse(
            success=True,
            message=f"已踢出群组并清理媒体账户。{result.message}",
        )
    except Exception as e:
        logger.exception("踢出用户失败: {}", e)
        raise HTTPException(status_code=500, detail="踢出用户操作失败") from e


@router.post(
    "/admin/user/{target_user_id}/ban",
    response_model=AdminActionResponse,
    dependencies=[Depends(validate_admin_access)],
)
async def admin_ban_user(
    request: Request,
    target_user_id: int,
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> AdminActionResponse:
    """管理员在群组中封禁目标用户"""
    try:
        await client.ban_user(target_user_id)
        return AdminActionResponse(
            success=True,
            message=f"已成功封禁该用户（ID: {target_user_id}）",
        )
    except Exception as e:
        logger.exception("封禁用户失败: {}", e)
        raise HTTPException(status_code=500, detail="封禁用户操作失败") from e


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
        operation=lambda: account_service.redeem_code(user_id, username, clean_code),
        dm_success_notice="兑换成功！详细信息已发送到您的 Telegram 私聊。",
        dm_failed_prefix="兑换成功",
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
                user_id, username, server_id, skip_checks=True
            )
        return await account_service.register(user_id, username, server_id)

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
        operation=lambda: service.forget_password(user_id, server_id),
        dm_success_notice="密码重置成功，新密码已发送到您的 Telegram 私聊。",
        dm_failed_prefix="密码重置成功",
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

    asyncio.create_task(
        run_subtitle_upload_flow(
            user_id, client, session, radarr_clients, sonarr_clients
        )
    )
    return {"success": True, "message": "请返回 Telegram 聊天窗口，查看上传指引。"}


# --- Request API (求片) ---


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
    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        return {"success": False, "message": "服务器不存在"}

    if not server.allow_request:
        return {"success": False, "message": "该服务器未开启求片功能"}

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
    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        return {"success": False, "message": "服务器不存在"}

    if not server.allow_request:
        return {"success": False, "message": "该服务器未开启求片功能"}

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
    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        return {"success": False, "message": "服务器不存在"}

    if not server.allow_request:
        return {"success": False, "message": "该服务器未开启求片功能"}

    service = RequestService(request.app, session)
    result = await service.submit_request_api(
        user_id, server_id, payload.library_name, payload.media_id
    )
    if not result.success:
        return {"success": False, "message": result.message}
    return {"success": True, "message": result.message}
