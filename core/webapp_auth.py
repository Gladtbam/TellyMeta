import hashlib
import hmac
import json
from urllib.parse import parse_qsl
from fastapi import Header, HTTPException, Request
from core.config import get_settings

settings = get_settings()

async def validate_admin_access(request: Request, x_telegram_init_data: str = Header(...)):
    """验证 Telegram WebApp 数据并检查管理员权限"""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Bot Token未配置")

    try:
        parsed_data = dict(parse_qsl(x_telegram_init_data))
        hash_check = parsed_data.pop('hash')

        # 1. 验证签名
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash != hash_check:
            raise HTTPException(status_code=401, detail="数据签名无效")

        # 2. 验证是否过期 (可选，检查 auth_date)

        # 3. 验证管理员权限
        user_data = json.loads(parsed_data['user'])
        user_id = user_data['id']

        # 从 app.state 中获取管理员列表 (需要在 main.py 中维护或查库)
        if user_id not in request.app.state.admin_ids:
            raise HTTPException(status_code=403, detail="未授权")

        return user_id

    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail="初始化数据格式无效") from e
