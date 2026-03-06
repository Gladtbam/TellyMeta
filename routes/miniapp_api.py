import asyncio
import contextlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.media import run_subtitle_upload_flow
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.database import get_db
from core.dependencies import (get_radarr_clients, get_sonarr_clients,
                               get_telethon_client)
from core.telegram_manager import TelethonClientWarper
from core.webapp_auth import get_current_user_id
from models.orm import RegistrationMode
from models.schemas import (AvailableServerDto, MediaAccountDto, MediaItemDto,
                            RequestLibraryDto, RequestSubmitDto,
                            ToggleResponse, UserInfoDto)
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.account_service import AccountService
from services.request_service import RequestService
from services.user_service import UserService

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])

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
                    tos=item.get("tos")
                ) for item in media_accounts_data
            ],
            available_servers=[
                AvailableServerDto(**item) for item in available_servers_data
            ]
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/redeem_code", response_model=ToggleResponse)
async def redeem_code(
    request: Request,
    code: str = Query(...),
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, bool | str]:
    """使用兑换码（注册码/续期码）"""
    username = await client.get_user_name(user_id, need_username=True)
    if not username or username is False:
        return {"success": False, "message": "请先设置 Telegram 用户名后再使用兑换码。"}

    account_service = AccountService(request.app, session)
    result = await account_service.redeem_code(user_id, username, code.strip())

    if result.success:
        await client.send_message(user_id, result.message, parse_mode='markdown')
        return {"success": True, "message": "兑换成功！详细信息已发送到您的 Telegram 私聊。"}

    return {"success": False, "message": result.message}

@router.post("/signup/{server_id}", response_model=ToggleResponse)
async def signup_account(
    request: Request,
    server_id: int,
    verification_input: str = Query(default=""),
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, bool | str]:
    """注册账户"""
    try:
        participant = await client.get_participant(user_id)
        if not participant:
            return {"success": False, "message": "您必须先加入群组才能注册账户。"}
    except Exception as e:
        logger.error(f"验证用户 {user_id} 群组身份失败: {e}")
        return {"success": False, "message": "验证群组身份时发生错误，请联系管理员。"}

    username = await client.get_user_name(user_id, need_username=True)
    if not username or username is False:
        return {"success": False, "message": "请先设置 Telegram 用户名后再注册。"}

    account_service = AccountService(request.app, session)

    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        return {"success": False, "message": "服务器不存在"}

    if server.registration_mode == RegistrationMode.EXTERNAL:
        if not verification_input:
            return {"success": False, "message": "该服务器需要验证字符串，请输入后重试。"}

        verify_result = await account_service.verify_external_user(server.id, verification_input)
        if not verify_result.success:
            return {"success": False, "message": verify_result.message}

        result = await account_service.register(user_id, username, server_id, skip_checks=True)
    else:
        result = await account_service.register(user_id, username, server_id)

    if result.success:
        await client.send_message(user_id, result.message, parse_mode='markdown')
        return {"success": True, "message": "注册成功！详细信息已发送到您的 Telegram 私聊。"}

    return {"success": False, "message": result.message}

@router.post("/accounts/{server_id}/toggle_nsfw", response_model=ToggleResponse)
async def toggle_account_nsfw(
    request: Request,
    server_id: int,
    user_id: int = Depends(get_current_user_id),
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
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, bool | str]:
    """重置账户密码"""
    service = AccountService(request.app, session)
    result = await service.forget_password(user_id, server_id)
    if not result.success:
        return {"success": False, "message": result.message}

    await client.send_message(user_id, result.message, parse_mode='markdown')

    return {"success": True, "message": "密码重置成功，新密码已发送到您的 Telegram 私聊。"}

@router.post("/accounts/{server_id}/renew", response_model=ToggleResponse)
async def renew_account(
    request: Request,
    server_id: int,
    user_id: int = Depends(get_current_user_id),
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
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> dict[str, bool | str]:
    """生成邀请码（注册码/续期码）"""
    if code_type not in ('signup', 'renew'):
        return {"success": False, "message": "无效的码类型"}

    service = AccountService(request.app, session)
    result = await service.generate_code(user_id, code_type, server_id)

    if result.success:
        # 将生成的码发送到用户私聊
        await client.send_message(user_id, result.message, parse_mode='markdown')
        return {"success": True, "message": "邀请码已生成，详情已发送到您的 Telegram 私聊。"}

    return {"success": False, "message": result.message}

@router.delete("/accounts/{server_id}", response_model=ToggleResponse)
async def delete_account(
    request: Request,
    server_id: int,
    user_id: int = Depends(get_current_user_id),
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
    user_id: int = Depends(get_current_user_id),
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

    asyncio.create_task(run_subtitle_upload_flow(user_id, client, session, radarr_clients, sonarr_clients))
    return {"success": True, "message": "请返回 Telegram 聊天窗口，查看上传指引。"}

# --- Request API (求片) ---

@router.get("/tools/{server_id}/request/libraries", response_model=list[RequestLibraryDto])
async def get_request_libraries(
    request: Request,
    server_id: int,
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
    user_id: int = Depends(get_current_user_id),
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
    result = await service.submit_request_api(user_id, server_id, payload.library_name, payload.media_id)
    if not result.success:
        return {"success": False, "message": result.message}
    return {"success": True, "message": result.message}
