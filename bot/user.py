from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import errors, events

from bot.decorators import provide_db_session
from bot.utils import safe_reply
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from services.notification_service import NotificationService
from services.user_service import UserService

settings = get_settings()


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=rf"^/checkin({settings.telegram_bot_name})?$", incoming=True
    )
)
@provide_db_session
async def checkin_handler(
    app: FastAPI, event: events.NewMessage.Event, session: AsyncSession
) -> None:
    """签到处理器：校验群组上下文、积分总开关与签到子开关后执行每日签到。

    Args:
        app: FastAPI 应用实例，用于获取 Telethon 客户端。
        event: Telegram `/checkin` 指令事件对象。
        session: 异步数据库会话。

    副作用：向群组回复签到结果，并在触发幸运奖励时向用户私聊下发激活码。
    """
    if event.chat_id != settings.telegram_chat_id:
        await safe_reply(event, "请在群组内签到。")
        return

    # 同时校验全局积分总开关（一票否决）与签到积分子开关
    allowed, reason = await ConfigRepository(session).check_points_feature(
        ConfigRepository.KEY_ENABLE_POINTS_CHECKIN
    )
    if not allowed:
        await safe_reply(event, reason)
        return

    client: TelethonClientWarper = app.state.telethon_client

    user_id = event.sender_id
    username = await client.get_user_name(user_id)

    user_service = UserService(app, session)
    result = await user_service.perform_checkin(user_id, username)

    if result.success:
        try:
            await event.reply(result.message, parse_mode="markdown")
        except (errors.FloodWaitError, errors.RPCError) as e:
            logger.warning("回复签到结果失败: {}", e)
    else:
        await safe_reply(event, result.message)

    if result.private_message:
        ok, err = await NotificationService(app).deliver_direct_dm(
            user_id=user_id,
            message=str(result.private_message),
            parse_mode="markdown",
            client=client,
            skip_inbox_fallback=True,
        )
        if not ok:
            logger.warning("向用户 {} 发送签到私聊消息失败: {}", user_id, err)
