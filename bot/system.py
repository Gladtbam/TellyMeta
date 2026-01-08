import asyncio
import textwrap
from typing import Any

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import errors, events

from bot.decorators import provide_db_session
from bot.utils import safe_reply, safe_respond, safe_respond_keyboard
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from services.score_service import MessageTrackingState, ScoreService
from services.user_service import UserService
from services.verification_service import VerificationService

settings = get_settings()

# 定义不需要计数的关键词
IGNORED_KEYWORDS = ['冒泡', '冒个泡', '好', '签到', '观看度']

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/start({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
async def start_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """欢迎消息处理器"""
    verification_service = VerificationService(app, session)
    challenge_data = await verification_service.create_get_challenge_details(event.sender_id)
    if challenge_data is None:
        await help_handler(app, event)  # 发送帮助消息
        return
    image_data, keyboard = challenge_data
    try:
        await event.respond(
            "欢迎！请在 **5 分钟内**选择下方正确答案：",
            file=image_data,
            buttons=keyboard
        )
    except errors.FloodWaitError as e:
        logger.warning("等待错误：等待 {} 秒", e.seconds)
        await asyncio.sleep(e.seconds)
        await event.respond(
            "欢迎！请在 **5 分钟内**选择下方正确答案：",
            file=image_data,
            buttons=keyboard
        )

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/help({settings.telegram_bot_name})?$',
    incoming=True
    ))
async def help_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """帮助消息处理器"""
    msg = textwrap.dedent("""\
    /help - [私聊]帮助
    /checkin - 签到
    /signup - 注册, 仅开放注册时使用
    /me - [私聊]查看 Emby 账户 和 个人 信息(包含其它工具)
    /code - [私聊]使用注册码注册, 或者使用续期码续期。例: /code 123
    /del - [管理员]删除 Emby 账户, 需回复一个用户
    /warn - [管理员]警告用户, 需回复一个用户
    /info - [管理员]查看用户信息
    /settle - [管理员]手动结算积分
    /change - [管理员]手动修改积分, 正数加负数减
    """)

    if event.is_private:
        await safe_respond(event, msg)
    else:
        await safe_reply(event, f'私聊我获取帮助: {settings.telegram_bot_name}', 20)

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/chat_id({settings.telegram_bot_name})?$',
    incoming=True
    ))
async def chat_id_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """群组ID处理器
    发送当前群组的ID，需在群组中使用
    """
    if event.is_private:
        await safe_reply(event, "请在群组中使用此命令以获取群组ID。")
    else:
        await safe_reply(event, f"当前群组ID: `{event.chat_id}`")

@TelethonClientWarper.handler(events.ChatAction(chats=settings.telegram_chat_id))
@provide_db_session
async def user_join_handler(app: FastAPI, event: events.ChatAction.Event, session: AsyncSession) -> None:
    """群组成员变动处理器
    处理新成员加入群组的事件
    """
    user_id: Any = event.user_id
    if not user_id:
        return
    if user_id == (await app.state.telethon_client.client.get_me()).id or user_id in app.state.admin_ids or user_id is None:
        return

    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_VERIFICATION, "true") != "true":
        return

    if event.user_joined or event.user_added:
        if event.user_added and event.added_by in app.state.admin_ids:
            logger.info("用户 {} 由管理员 {} 邀请，已跳过验证流程。", user_id, event.added_by)
            return
        logger.info("用户 {} 加入", user_id)
        verification_service = VerificationService(app, session)
        result = await verification_service.start_verification(user_id)

        if not result.success:
            return

        message = await safe_respond_keyboard(event, result.message, result.keyboard, 300)
        if message and message.id:
            await verification_service.verification_repo.update_message_id(user_id, message.id)

    if event.user_left or event.user_kicked:
        logger.info("用户 {} 离开", user_id)
        user_service = UserService(app, session)
        await user_service.delete_account(user_id, 'both')

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'verify_(\\d+)'))
@provide_db_session
async def verify_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """验证码处理器
    处理用户点击验证码按钮的事件
    """
    user_id: Any = event.sender_id
    answer = event.pattern_match.group(1).decode('utf-8') # type: ignore

    verification_service = VerificationService(app, session)
    client: TelethonClientWarper = app.state.telethon_client
    result = await verification_service.process_verifocation_attempt(user_id, answer)

    await safe_respond(event, result.message)
    if result.success and result.private_message and isinstance(result.private_message, int):
        await client.edit_message(settings.telegram_chat_id, result.private_message, "您已通过验证，可以在群组中发言了。")

@TelethonClientWarper.handler(events.NewMessage(chats=settings.telegram_chat_id))
@provide_db_session
async def group_message_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """群组消息处理器
    根据消息类型，计算积分，连续发送不计入积分
    """
    if not event.sender_id or not event.message or not event.message.text:
        return # 忽略无发送者或无文本的消息

    user_id = event.sender_id

    if user_id in app.state.admin_ids:
        return

    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_POINTS, "true") != "true":
        return

    if not event.message.text.startswith('/'):
        return

    if not any(word in event.message.text for word in IGNORED_KEYWORDS):
        return

    flood_state: MessageTrackingState = app.state.message_tracker
    antiflood_service = ScoreService(session, flood_state)
    flood_result = await antiflood_service.process_message(user_id)
    if flood_result:
        await safe_reply(event, flood_result.message)

@TelethonClientWarper.handler(events.NewMessage(
    pattern=r'^/(\w+)(?:@\w+)?$',
    incoming=True
    ))
async def unknown_command_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """未知命令处理器
    处理未知命令，提示用户使用 /help 获取帮助
    删除所有命令消息
    """
    known_commands = [
        'start', 'help', 'me', 'info', 'chat_id', 'del', 'code',
        'checkin', 'warn', 'change', 'settle', 'signup', 'settings',
        'kick', 'ban'
    ]
    try:
        command = event.pattern_match.group(1).lower()  # type: ignore
        if command not in known_commands:
            await safe_reply(event, f"未知命令: /{command}. 使用 /help 获取帮助。")
    except IndexError:
        logger.warning("group(1) 不存在")

    try:
        await asyncio.sleep(1)
        await event.delete()
    except errors.FloodWaitError as e:
        logger.warning("删除消息时等待错误：等待{}秒", e.seconds)
        await asyncio.sleep(e.seconds)
        await event.delete()
