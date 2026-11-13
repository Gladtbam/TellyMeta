import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.dependencies import get_telethon_client
from core.telegram_manager import TelethonClientWarper
from repositories.media_repo import MediaRepository

settings = get_settings()

# Telegram Mini App initData 有效时长：1 小时 (3600 秒)，允许 5 分钟 (300 秒) 时钟偏差
INIT_DATA_MAX_AGE_SECONDS = 3600
INIT_DATA_MAX_FUTURE_SKEW_SECONDS = 300


async def validate_init_data(x_telegram_init_data: str) -> dict:
    """验证 Telegram MiniApp 数据签名与有效时长，并返回用户信息。"""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Bot Token未配置")

    try:
        parsed_data = dict(parse_qsl(x_telegram_init_data))
        if "hash" not in parsed_data:
            raise HTTPException(status_code=401, detail="缺少 hash 字段")

        hash_check = parsed_data.pop("hash")

        # 使用常量时间比较验证 HMAC-SHA256 签名
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        secret_key = hmac.new(
            b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, hash_check):
            raise HTTPException(status_code=401, detail="数据签名无效")

        # 校验 auth_date 防止过期凭据被重放
        auth_date_raw = parsed_data.get("auth_date")
        if not auth_date_raw:
            raise HTTPException(status_code=401, detail="缺少 auth_date 字段")
        try:
            auth_date = int(auth_date_raw)
        except ValueError as e:
            raise HTTPException(status_code=401, detail="无效的 auth_date 字段") from e

        now_ts = int(time.time())
        if auth_date - now_ts > INIT_DATA_MAX_FUTURE_SKEW_SECONDS:
            raise HTTPException(status_code=401, detail="身份凭据时间异常")
        if now_ts - auth_date > INIT_DATA_MAX_AGE_SECONDS:
            raise HTTPException(
                status_code=401, detail="身份凭据已过期，请重新打开 Mini App"
            )

        user_data = json.loads(parsed_data.get("user", "{}"))
        if not user_data or "id" not in user_data:
            raise HTTPException(status_code=401, detail="无效的用户数据")

        return user_data

    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"数据验证失败: {str(e)}") from e


async def get_current_user_id(x_telegram_init_data: str = Header(...)) -> int:
    """获取已验证且未过期的当前 WebApp 用户 ID。"""
    user_data = await validate_init_data(x_telegram_init_data)
    return int(user_data["id"])


async def require_group_member(
    user_id: int = Depends(get_current_user_id),
    client: TelethonClientWarper = Depends(get_telethon_client),
) -> int:
    """验证当前用户属于主 Telegram 群组成员，否则拒绝成员自助操作。"""
    try:
        participant = await client.get_participant(user_id)
    except Exception as e:
        raise HTTPException(
            status_code=503, detail="验证主群成员身份失败，请稍后重试"
        ) from e

    if not participant:
        raise HTTPException(
            status_code=403, detail="您当前不在主群组中，无法使用自助服务"
        )
    return user_id


async def require_owned_media_account(
    server_id: int,
    user_id: int = Depends(require_group_member),
    session: AsyncSession = Depends(get_db),
) -> int:
    """验证当前主群成员在目标媒体服务器上拥有归属自己的媒体账户。"""
    media_user = await MediaRepository(session).get_by_id(user_id, server_id)
    if media_user is None:
        raise HTTPException(
            status_code=403, detail="您在该服务器没有媒体账户，无权执行此操作"
        )
    return user_id


async def validate_admin_access(
    request: Request, x_telegram_init_data: str = Header(...)
) -> int:
    """验证管理员权限（fail-closed：未配置 admin_ids 时一律拒绝）。"""
    user_data = await validate_init_data(x_telegram_init_data)
    user_id = int(user_data["id"])

    admin_ids = getattr(request.app.state, "admin_ids", None)
    if not admin_ids or user_id not in admin_ids:
        raise HTTPException(status_code=403, detail="未授权: 需要管理员权限")

    return user_id
