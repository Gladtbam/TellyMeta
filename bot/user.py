from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import events

from bot.decorators import provide_db_session
from bot.utils import safe_reply
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from services.user_service import UserService

settings = get_settings()


@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/checkin({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
async def checkin_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """签到处理器"""  
    if event.chat_id != settings.telegram_chat_id:
        await safe_reply(event, "请在群组内签到。")
        return

    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_POINTS, "true") != "true":
        await safe_reply(event, "签到功能已关闭。")
        return

    user_id = event.sender_id
    user_service = UserService(app, session)
    result = await user_service.perform_checkin(user_id)

    await safe_reply(event, result.message)

    if result.private_message:
        client: TelethonClientWarper = app.state.telethon_client
        await client.send_message(user_id, str(result.private_message))
