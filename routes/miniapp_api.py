import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bot.media import run_subtitle_upload_flow
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.database import get_db
from core.dependencies import (get_radarr_clients, get_sonarr_clients,
                               get_telethon_client)
from core.telegram_manager import TelethonClientWarper
from core.webapp_auth import get_current_user_id
from models.schemas import (MediaAccountDto, MediaItemDto, RequestLibraryDto,
                            RequestSubmitDto, ToggleResponse, UserInfoDto)
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
):
    """获取当前 MiniApp 用户信息"""
    service = UserService(request.app, session)
    telegram_repo = TelegramRepository(session)
    try:
        data = await service.get_user_info_data(user_id)
        renew_score = await telegram_repo.get_renew_score()

        user = data["user"]
        media_accounts_data = data["media_accounts"]

        is_admin = user.id in getattr(request.app.state, "admin_ids", set())

        return UserInfoDto(
            id=user.id,
            score=user.score,
            checkin_count=user.checkin_count,
            warning_count=user.warning_count,
            renew_score=int(renew_score),
            is_admin=is_admin,
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
                    allow_request=item.get("allow_request")
                ) for item in media_accounts_data
            ]
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/accounts/{server_id}/toggle_nsfw", response_model=ToggleResponse)
async def toggle_account_nsfw(
    request: Request,
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
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
):
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
):
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
):
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

@router.post("/tools/{server_id}/upload_subtitle", response_model=ToggleResponse)
async def trigger_upload_subtitle(
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    client: TelethonClientWarper = Depends(get_telethon_client),
    radarr_clients: dict[int, RadarrClient] = Depends(get_radarr_clients),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
):
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
):
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
