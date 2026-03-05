from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import events

from bot.decorators import provide_db_session
from bot.utils import safe_reply, safe_respond
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from services.account_service import AccountService
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

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/code({settings.telegram_bot_name})?(\s.+)?$',
    incoming=True
    ))
@provide_db_session
async def code_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """激活码处理器
    处理用户使用激活码注册或续期的请求
    """
    if not event.is_private:
        await safe_reply(event, "请私聊我以使用码。")
        return

    try:
        args_str = event.pattern_match.group(2).strip() # type: ignore
    except (IndexError, AttributeError):
        await safe_reply(event, "请在命令后添加激活码，例如: /code YOUR_CODE")
        return

    user_id = event.sender_id
    client: TelethonClientWarper = app.state.telethon_client
    user_name = await client.get_user_name(user_id, need_username=True)

    account_service = AccountService(app, session)
    result = await account_service.redeem_code(user_id, user_name, args_str)
    await safe_respond(event, result.message)
