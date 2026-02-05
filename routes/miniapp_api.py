from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.dependencies import get_telethon_client
from core.telegram_manager import TelethonClientWarper
from core.webapp_auth import get_current_user_id
from models.schemas import MediaAccountDto, ToggleResponse, UserInfoDto
from repositories.telegram_repo import TelegramRepository
from services.account_service import AccountService
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
                    is_banned=item["is_banned"]
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
