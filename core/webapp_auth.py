import hashlib
import hmac
import json
from urllib.parse import parse_qsl
from fastapi import Header, HTTPException, Request
from core.config import get_settings

settings = get_settings()

async def validate_init_data(x_telegram_init_data: str) -> dict:
    """验证 Telegram MiniApp 数据并返回用户信息"""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Bot Token未配置")

    try:
        parsed_data = dict(parse_qsl(x_telegram_init_data))
        if 'hash' not in parsed_data:
            raise HTTPException(status_code=401, detail="缺少 hash 字段")

        hash_check = parsed_data.pop('hash')

        # 验证签名
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash != hash_check:
            raise HTTPException(status_code=401, detail="数据签名无效")

        user_data = json.loads(parsed_data.get('user', '{}'))
        if not user_data or 'id' not in user_data:
            raise HTTPException(status_code=401, detail="无效的用户数据")

        return user_data

    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"数据验证失败: {str(e)}") from e

async def get_current_user_id(x_telegram_init_data: str = Header(...)) -> int:
    """获取当前 WebApp 用户 ID"""
    user_data = await validate_init_data(x_telegram_init_data)
    return user_data['id']

async def validate_admin_access(request: Request, x_telegram_init_data: str = Header(...)) -> int:
    """验证管理员权限"""
    user_data = await validate_init_data(x_telegram_init_data)
    user_id = user_data['id']

    # 检查管理员权限
    if hasattr(request.app.state, 'admin_ids') and user_id not in request.app.state.admin_ids:
        raise HTTPException(status_code=403, detail="未授权: 需要管理员权限")

    return user_id
