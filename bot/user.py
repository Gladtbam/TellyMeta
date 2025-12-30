import textwrap
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, events

from bot.decorators import provide_db_session
from bot.utils import safe_reply, safe_respond, safe_respond_keyboard
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository
from services.account_service import AccountService
from services.user_service import Result, UserService

settings = get_settings()


@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/me({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
async def me_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """用户信息处理器
    发送用户信息和交互按钮，私聊仅发送用户信息
    """
    if not event.is_private:
        await safe_reply(event, f'私聊我获取个人信息: {settings.telegram_bot_name}')
        return

    user_service = UserService(app, session)
    user_id = event.sender_id

    result = await user_service.get_user_info(user_id)
    await safe_respond_keyboard(event, result.message, result.keyboard)

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
    pattern=fr'^/signup({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
async def signup_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """注册处理器
    处理用户注册请求，仅在开放注册时允许
    """
    if not event.is_private:
        await safe_reply(event, "请私聊我以注册账户。")
        return

    account_service = AccountService(app, session)
    result = await account_service.get_register_servers_keyboard()
    if not result.success:
        await safe_respond(event, result.message)
    else:
        await event.respond(result.message, result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'signup_srv_(\\d+)'))
@provide_db_session
async def signup_confirm_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """确认注册"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    user_id: Any = event.sender_id
    client: TelethonClientWarper = app.state.telethon_client

    user_name = await client.get_user_name(user_id, need_username=True)
    if not user_name:
        await event.answer("请先设置 Telegram 用户名", alert=True)
        return

    await event.answer("正在注册...", alert=False)

    account_service = AccountService(app, session)
    result = await account_service.register(user_id, user_name, server_id)
    await safe_respond(event, result.message)

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

@TelethonClientWarper.handler(events.CallbackQuery(data=b'me_create_code'))
@provide_db_session
async def create_code_start_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """开始生成码：选择服务器"""
    service = AccountService(app, session)
    result = await service.get_server_selection_for_code("create_code_srv")

    if not result.success:
        await event.answer(result.message, alert=True)
    else:
        await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'create_code_srv_(\\d+)'))
@provide_db_session
async def create_code_type_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """选择码类型"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    telegram_repo = TelegramRepository(session)
    score = await telegram_repo.get_renew_score()

    keyboard = [
        [Button.inline("注册码 (Signup)", data=f"create_code_fin_{server_id}_signup".encode())],
        [Button.inline("续期码 (Renew)", data=f"create_code_fin_{server_id}_renew".encode())]
    ]
    msg = textwrap.dedent(f"""\
        生成码需要消耗 **{score}** 积分。
        请选择要生成的码类型：
        - 续期码：用于续期现有账户。
        - 注册码：用于注册新账户。
        """)
    await event.edit(msg, buttons=keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'create_code_fin_(\\d+)_(signup|renew)'))
@provide_db_session
async def create_code_finish_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """执行生成"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    ctype = event.pattern_match.group(2).decode() # type: ignore
    user_id: Any = event.sender_id

    service = AccountService(app, session)
    result = await service.generate_code(user_id, ctype, server_id)

    await event.respond(result.message)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'me_(renew|nsfw|forget_password|query_renew)'))
@provide_db_session
async def me_action_init_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """续期/NSFW/忘记密码处理器/查询续期积分
    处理用户点击续期/NSFW/忘记密码按钮的事件"""
    user_id: Any = event.sender_id
    action = event.pattern_match.group(1).decode('utf-8') # type: ignore
    account_service = AccountService(app, session)

    result = await account_service.get_user_accounts_keyboard(user_id, f"me_do_{action}")

    if not result.success:
        await event.answer(result.message, alert=True)
    else:
        await safe_respond_keyboard(event, result.message, result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'me_do_(renew|nsfw|forget_password|query_renew)_(\\d+)'))
@provide_db_session
async def me_action_exec_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """执行个人中心具体操作 (指定服务器)"""
    action = event.pattern_match.group(1).decode() # type: ignore
    server_id = int(event.pattern_match.group(2).decode()) # type: ignore
    user_id: Any = event.sender_id
    account_service = AccountService(app, session)

    if action == 'renew':
        result = await account_service.renew(user_id, server_id, use_score=True)
    elif action == 'nsfw':
        result = await account_service.toggle_nsfw_policy(user_id, server_id)
    elif action == 'forget_password':
        result = await account_service.forget_password(user_id, server_id)
        if result.success:
            await event.respond(result.message, parse_mode='markdown') # 密码需要发送新消息以便复制
            await event.answer("重置成功")
            return
    elif action == 'query_renew':
        telegram_repo = TelegramRepository(session)
        renew_score = int(await telegram_repo.get_renew_score())
        result = Result(True, f"当前续期积分为 {renew_score}")
    else:
        result = Result(False, "未知操作。")

    await safe_respond(event, result.message)
