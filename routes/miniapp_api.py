from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.webapp_auth import get_current_user_id
from models.schemas import MediaAccountDto, UserInfoDto
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
    try:
        data = await service.get_user_info_data(user_id)

        user = data["user"]
        media_accounts_data = data["media_accounts"]

        is_admin = user.id in getattr(request.app.state, "admin_ids", set())

        return UserInfoDto(
            id=user.id,
            score=user.score,
            checkin_count=user.checkin_count,
            warning_count=user.warning_count,
            is_admin=is_admin,
            media_accounts=[
                MediaAccountDto(
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
