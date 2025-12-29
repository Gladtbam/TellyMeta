import asyncio
import base64
import textwrap
from typing import Any

import aiofiles.tempfile
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, errors, events

from bot.decorators import provide_db_session, require_admin
from bot.utils import (get_user_input_or_cancel, safe_reply, safe_respond,
                       safe_respond_keyboard)
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.orm import ServerType
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.account_service import AccountService
from services.request_service import RequestService
from services.score_service import MessageTrackingState, ScoreService
from services.settings_service import SettingsServices
from services.subtitle_service import SubtitleService
from services.user_service import Result, UserService
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
    pattern=fr'^/me({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
async def me_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """用户信息处理器
    发送用户信息和交互按钮，私聊仅发送用户信息
    """
    user_service = UserService(app, session)

    user_id = None
    if event.is_reply:
        user_id = (await event.get_reply_message()).sender_id
    else:
        user_id = event.sender_id

    result = await user_service.get_user_info(user_id)

    if event.is_private:
        if result.keyboard:
            await safe_respond_keyboard(event, result.message, result.keyboard)
        else:
            await safe_respond(event, result.message)
    elif event.sender_id in app.state.admin_ids and user_id != event.sender_id:
        await safe_reply(event, result.message)
    else:
        await safe_reply(event, f'私聊我获取个人信息: {settings.telegram_bot_name}')

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/info({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
@require_admin
async def info_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """用户信息处理器
    发送用户信息，需回复一个用户
    """

    if not event.is_reply:
        await safe_reply(event, "请回复一个用户以查看其信息。")
    await me_handler(app, event, session)

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
    pattern=fr'^/warn({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
@require_admin
async def warn_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """警告处理器
    警告一个用户，需回复一个用户
    """
    if not event.is_reply:
        await safe_reply(event, "请回复一个用户以警告。")
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg.sender_id:
        await safe_reply(event, "无法获取回复的用户信息。")
        return

    target_user_id = reply_msg.sender_id

    user_service = UserService(app, session)
    user = await user_service.telegram_repo.update_warn_and_score(target_user_id)

    await safe_reply(event, f"✅ 用户 [{user.id}](tg://user?id={user.id}) 已被警告，当前警告次数: **{user.warning_count}**。")

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/change({settings.telegram_bot_name})?\s+(-?\d+)$',
    incoming=True
    ))
@provide_db_session
@require_admin
async def change_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """修改积分处理器
    修改一个用户的积分，需回复一个用户并在命令后添加积分数
    """
    if not event.is_reply:
        await safe_reply(event, "请回复一个用户以修改其积分。")
        return

    args = event.message.text.split()
    if len(args) != 2:
        await safe_reply(event, "请在命令后添加积分数，例如: /change 10 或 /change -5")
        return

    score_change = int(args[1])

    reply_msg = await event.get_reply_message()
    if not reply_msg.sender_id:
        await safe_reply(event, "无法获取回复的用户信息。")
        return

    target_user_id = reply_msg.sender_id

    user_service = UserService(app, session)
    user = await user_service.telegram_repo.update_score(target_user_id, score_change)

    await safe_reply(event, f"✅ 用户 [{user.id}](tg://user?id={user.id}) 的积分已修改，当前积分: **{user.score}**。")

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/settle({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
@require_admin
async def settle_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """积分结算处理器
    手动触发积分结算
    """
    score_service = ScoreService(session, app.state.message_tracker)
    result = await score_service.settle_and_clear_scores()
    client: TelethonClientWarper = app.state.telethon_client

    if result is None:
        await safe_reply(event, "当前无积分可结算。")
        return

    summary = textwrap.dedent(f"""\
        ✅ 积分结算完成！
        共结算 **{result.total_score_settled}** 活跃度积分.
        本次结算详情:
        """)
    summary_msg = await client.send_message(settings.telegram_chat_id, summary)

    user_details = []
    for user_id, score_change in result.user_score_changes.items(): # type: ignore
        username = await client.get_user_name(user_id)
        user_details.append(f"- [{username}](tg://user?id={user_id}): `+{score_change}`")
    final_summary = summary + "\n".join(user_details)
    await client.client.edit_message(summary_msg, final_summary)

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/del({settings.telegram_bot_name})?$',
    incoming=True
    ))
@provide_db_session
@require_admin
async def delete_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """删除账户处理器
    删除一个用户的 Emby 账户，需回复一个用户
    """
    if not event.is_reply:
        await safe_reply(event, "请回复一个用户以删除其账户。")
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg.sender_id:
        await safe_reply(event, "无法获取回复的用户信息。")
        return

    target_user_id = reply_msg.sender_id

    user_service = UserService(app, session)
    result = await user_service.delete_account(target_user_id, 'both')

    await safe_reply(event, result.message)

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/kick({settings.telegram_bot_name})?$',
    incoming=True
    ))
@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'kick_(\\d+)'))
@provide_db_session
@require_admin
async def kick_handler(app: FastAPI, event: Any, session: AsyncSession) -> None:
    """踢出处理器
    踢出一个用户，支持命令和按钮两种触发方式
    """

    if isinstance(event, events.NewMessage.Event):
        if not event.is_reply:
            await safe_reply(event, "请回复一个用户以踢出。")
            return
        reply_msg = await event.get_reply_message()
        if not reply_msg.sender_id:
            await safe_reply(event, "无法获取回复的用户信息。")
            return

        user_service = UserService(app, session)
        client: TelethonClientWarper = app.state.telethon_client
        target_user_id = reply_msg.sender_id
        await client.kick_and_ban_participant(target_user_id)
        result = await user_service.delete_account(target_user_id, 'both')
        await safe_reply(event, '已踢出用户。\n' + result.message)
    elif isinstance(event, events.CallbackQuery.Event):
        target_user_id = int(event.pattern_match.group(1).decode('utf-8')) # type: ignore
        verification_service = VerificationService(app, session)
        result = await verification_service.reject_verification(target_user_id)
        await event.edit(result.message)
    else:
        await safe_respond(event, "无法处理此事件类型。")
        return

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/ban({settings.telegram_bot_name})?$',
    incoming=True
    ))
@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'ban_(\\d+)'))
@provide_db_session
@require_admin
async def ban_handler(app: FastAPI, event: Any, session: AsyncSession) -> None:
    """封禁处理器
    封禁一个用户，支持命令和按钮两种触发方式
    """

    if isinstance(event, events.NewMessage.Event):
        if not event.is_reply:
            await safe_reply(event, "请回复一个用户以封禁。")
            return
        reply_msg = await event.get_reply_message()
        if not reply_msg.sender_id:
            await safe_reply(event, "无法获取回复的用户信息。")
            return

        client: TelethonClientWarper = app.state.telethon_client
        target_user_id = reply_msg.sender_id
        user_name = await client.get_user_name(target_user_id)
        await client.ban_user(target_user_id)
        await safe_reply(event, f'已封禁用户[{user_name}](tg://user?id={target_user_id})')
    elif isinstance(event, events.CallbackQuery.Event):
        target_user_id = int(event.pattern_match.group(1).decode('utf-8')) # type: ignore
        verification_service = VerificationService(app, session)
        result = await verification_service.reject_verification(target_user_id, is_ban=True)
        await event.edit(result.message)
    else:
        await safe_respond(event, "无法处理此事件类型。")
        return

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

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/settings({settings.telegram_bot_name})?$',
    incoming=True
))
@provide_db_session
@require_admin
async def settings_handler(app: FastAPI, event: events.NewMessage.Event, session: AsyncSession) -> None:
    """设置处理器
    处理管理员请求设置面板
    """
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_admin_management_keyboard()

    await safe_respond_keyboard(event, result.message, result.keyboard, 600)
    logger.info("管理员 {} 请求设置面板", event.sender_id)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'toggle_system_(.+)'))
@provide_db_session
async def toggle_system_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """系统功能开关处理器"""
    key = event.pattern_match.group(1).decode('utf-8') # type: ignore
    settings_service = SettingsServices(app, session)

    result = await settings_service.toggle_system_setting(key)
    await event.answer(result.message)

    # 刷新面板
    panel_result = await settings_service.get_system_panel()
    await event.edit(panel_result.message, buttons=panel_result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(
    pattern=b'manage_(admins|notify|media|system|main)'))
@provide_db_session
async def manage_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """管理面板处理器
    处理管理员点击管理面板按钮的事件
    一级菜单
    """
    action = event.pattern_match.group(1).decode('utf-8') # type: ignore
    settings_service = SettingsServices(app, session)

    if action == 'admins':
        result: Result = await settings_service.get_admins_panel()
    elif action == 'notify':
        result = await settings_service.get_notification_panel()
    elif action == 'media':
        result = await settings_service.get_media_panel()
    elif action == 'system':
        result = await settings_service.get_system_panel()
    elif action == 'main':
        result = await settings_service.get_admin_management_keyboard()
    else:
        result = Result(False, "该功能尚未实现。")

    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'view_server_(\\d+)'))
@provide_db_session
async def view_server_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """查看服务器详情"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_server_detail_panel(server_id)
    if not result.success:
        await event.answer(result.message, alert=True)
    else: await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'delete_server_confirm_(\\d+)'))
@provide_db_session
async def delete_server_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """删除服务器"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.delete_server(server_id)
    await event.answer(result.message, alert=True)
    # 返回列表
    result = await settings_service.get_media_panel()
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_nsfw_toggle_(\\d+)'))
@provide_db_session
async def srv_nsfw_toggle_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """切换 NSFW 开关"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.toggle_server_nsfw(server_id)
    await event.answer(result.message)
    # 刷新
    result = await settings_service.get_server_detail_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_nsfw_libs_(\\d+)'))
@provide_db_session
async def srv_nsfw_libs_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """打开 NSFW 库选择面板"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_nsfw_library_panel(server_id)
    if not result.success:
        await event.answer(result.message, alert=True)
    else: await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_nsfw_setlib_(\\d+)_(.+)'))
@provide_db_session
async def srv_nsfw_setlib_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """切换单个 NSFW 库状态"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    lib_b64 = event.pattern_match.group(2).decode() # type: ignore
    lib_id = base64.b64decode(lib_b64).decode()

    settings_service = SettingsServices(app, session)
    await settings_service.toggle_nsfw_library(server_id, lib_id)
    # 刷新
    result = await settings_service.get_nsfw_library_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'toggle_admin_(\\d+)'))
@provide_db_session
async def toggle_admin_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """切换管理员处理器
    处理管理员点击切换管理员按钮的事件
    """
    user_id = int(event.pattern_match.group(1).decode('utf-8')) # type: ignore
    settings_service = SettingsServices(app, session)

    result = await settings_service.toggle_admin(user_id)
    await event.answer(result.message)

    # 刷新管理员面板
    panel_result = await settings_service.get_admins_panel()
    await event.edit(panel_result.message, buttons=panel_result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_expiry_(\\d+)'))
@provide_db_session
async def srv_expiry_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """打开有效期设置"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_registration_expiry_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_set_exp_(\\d+)_(\\d+)'))
@provide_db_session
async def srv_set_exp_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """设置有效期"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    days = int(event.pattern_match.group(2).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    await settings_service.set_registration_expiry(server_id, days)
    await event.answer(f"已设为 {days} 天")
    # 返回详情
    result = await settings_service.get_server_detail_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'^notify_(sonarr|radarr|media|requested)'))
@provide_db_session
async def notify_setting_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """通知设置处理器
    处理管理员点击通知设置按钮的事件
    """
    setting_type = event.pattern_match.group(1).decode('utf-8') # type: ignore

    settings_service = SettingsServices(app, session)
    result = await settings_service.get_notification_keyboard(setting_type)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'^set_notify_(sonarr|radarr|media|requested)_(-?\\d+)'))
@provide_db_session
async def set_notify_topic_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """设置通知话题处理器
    处理管理员点击设置通知话题按钮的事件
    """
    setting_type = event.pattern_match.group(1).decode('utf-8') # type: ignore
    topic_id = int(event.pattern_match.group(2).decode('utf-8')) # type: ignore

    settings_service = SettingsServices(app, session)
    result = await settings_service.set_notification_topic(setting_type, topic_id)

    await event.answer(result.message)

    # 刷新通知设置面板
    notify_result = await settings_service.get_notification_panel()
    await event.edit(notify_result.message, buttons=notify_result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'manage_libs_(\\d+)'))
@provide_db_session
async def manage_libs_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """查看某服务器的库列表"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_server_libraries_panel(server_id)
    if not result.success:
        await event.answer(result.message, alert=True)
    else: await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'bind_lib_menu_(.+)'))
@provide_db_session
async def bind_lib_menu_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """单个库的绑定菜单"""
    lib_name = base64.b64decode(event.pattern_match.group(1)).decode() # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_library_binding_menu(lib_name)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'bind_sel_server_(.+)'))
@provide_db_session
async def bind_sel_server_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """选择下载器实例列表"""
    lib_name = base64.b64decode(event.pattern_match.group(1)).decode() # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_arr_server_selection(lib_name)
    if not result.success:
        await event.answer(result.message, alert=True)
    else: await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'bind_set_srv_(\\d+)_(.+)'))
@provide_db_session
async def bind_set_srv_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """执行绑定实例"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    lib_name = base64.b64decode(event.pattern_match.group(2)).decode() # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.bind_server_to_library(lib_name, server_id)
    await event.answer(result.message)
    # 返回绑定菜单
    result = await settings_service.get_library_binding_menu(lib_name)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'bind_sel_(quality|folder)_(.+)'))
@provide_db_session
async def bind_sel_conf_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """选择质量/文件夹"""
    target = event.pattern_match.group(1).decode() # type: ignore
    lib_name = base64.b64decode(event.pattern_match.group(2)).decode() # type: ignore
    settings_service = SettingsServices(app, session)

    if target == 'quality':
        result = await settings_service.get_quality_selection(lib_name)
    else:
        result = await settings_service.get_folder_selection(lib_name)

    if not result.success:
        await event.answer(result.message, alert=True)
    else: await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'bind_set_(quality|folder)_(.+)_(.+)'))
@provide_db_session
async def bind_set_conf_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """执行设置质量/文件夹"""
    target = event.pattern_match.group(1).decode() # type: ignore
    value_raw = event.pattern_match.group(2).decode() # type: ignore
    lib_name = base64.b64decode(event.pattern_match.group(3)).decode() # type: ignore

    settings_service = SettingsServices(app, session)

    if target == 'folder':
        try:
            folder_id = int(value_raw)
            result = await settings_service.set_library_root_folder_by_id(lib_name, folder_id)
        except ValueError:
             result = Result(False, "无效的文件夹 ID 数据")
    else:
        try:
            value = int(value_raw)
            result = await settings_service.set_library_binding(lib_name, 'quality_profile_id', value)
        except ValueError:
            result = Result(False, "无效的质量配置 ID")
    await event.answer(result.message)
    # 返回绑定菜单
    result = await settings_service.get_library_binding_menu(lib_name)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_reg_mode_(\\d+)'))
@provide_db_session
async def srv_reg_mode_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """进入注册模式面板"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)
    result = await settings_service.get_registration_mode_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_set_mode_(\\d+)_(default|open|close)'))
@provide_db_session
async def srv_set_mode_simple_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """设置简单模式 (默认/开放/关闭)"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    mode = event.pattern_match.group(2).decode() # type: ignore

    settings_service = SettingsServices(app, session)
    result = await settings_service.set_server_registration_mode(server_id, mode)

    await event.answer(result.message)
    # 刷新面板
    result = await settings_service.get_registration_mode_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_input_mode_(\\d+)_(count|time)'))
@provide_db_session
async def srv_input_mode_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """设置复杂模式 (名额/时间)，触发对话"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    target = event.pattern_match.group(2).decode() # type: ignore

    chat_id = event.chat_id
    client = app.state.telethon_client.client

    prompt = ""
    if target == "count":
        prompt = "请输入开放注册的名额数量 (纯数字，例如 `50`)："
    else:
        prompt = "请输入开放时长 (格式如 `1h`, `30m`, `1h30m`)："

    try:
        async with client.conversation(chat_id, timeout=60) as conv:
            cancel_btn = [Button.inline("取消", b"srv_mode_cancel")]
            prompt_msg = await conv.send_message(prompt, buttons=cancel_btn)

            input_val = await get_user_input_or_cancel(conv, prompt_msg.id)

            if not input_val:
                try:
                    await prompt_msg.delete()
                except:
                    pass
                return

            try:
                await prompt_msg.delete()
            except:
                pass

            settings_service = SettingsServices(app, session)
            result = await settings_service.set_server_registration_mode(server_id, input_val)

            if result.success:
                await event.answer("设置成功")
                # 刷新原消息面板
                panel = await settings_service.get_registration_mode_panel(server_id)
                await event.edit(panel.message, buttons=panel.keyboard)
                return
            else:
                await event.answer(f"设置失败: {result.message}，请重试！", alert=True)

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或点击之前的【取消】按钮，或发送 /cancel 指令。", alert=True)
    except asyncio.TimeoutError:
        await event.answer("操作超时", alert=True)
    except Exception as e:
        logger.error(f"Conversation error: {e}")
        await event.answer("发生错误，请重试", alert=True)

@TelethonClientWarper.handler(events.CallbackQuery(data=b'req_cancel'))
async def request_cancel_handler(app: FastAPI, event: events.CallbackQuery.Event) -> None:
    """求片-取消处理器"""
    await event.delete()

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'^req_ap_([^_]+)_(\\d+)'))
@provide_db_session
@require_admin
async def request_approve_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """求片-批准处理器"""
    library_name_base64 = event.pattern_match.group(1).decode('utf-8') # type: ignore
    media_id = int(event.pattern_match.group(2).decode('utf-8')) # type: ignore

    library_name = base64.b64decode(library_name_base64.encode('utf-8')).decode('utf-8')

    request_service = RequestService(app, session)
    # Give admin immediate feedback
    await event.answer("正在添加中...", alert=False)

    result = await request_service.handle_approval(library_name, media_id)

    if result.success:
        # Edit the message to show approved status and remove buttons
        original_text = (await event.get_message()).text  # type: ignore
        new_text = original_text + f"\n\n✅ **已批准**: {result.message}"
        await event.edit(new_text, buttons=None)
    else:
        await event.answer(result.message, alert=True)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'^req_deny_(\\d+)'))
@require_admin
async def request_deny_handler(app: FastAPI, event: events.CallbackQuery.Event) -> None:
    """求片-拒绝处理器"""
    # user_id = int(event.pattern_match.group(1).decode('utf-8'))
    # Optional: Notify user
    original_text = (await event.get_message()).text # type: ignore
    new_text = original_text + "\n\n❌ **已拒绝**"
    await event.edit(new_text, buttons=None)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'^me_request_(\\d+)'))
@provide_db_session
async def start_request_conversation_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """开始求片处理器 (Conversation Mode)"""
    user_id = int(event.pattern_match.group(1).decode('utf-8')) # type: ignore
    chat_id = event.chat_id
    request_service = RequestService(app, session)
    client = app.state.telethon_client.client

    # 检查权限
    # start_request_flow 将检查权限并返回库按钮
    result = await request_service.start_request_flow(user_id)

    if not result.success:
        await event.answer(result.message, alert=True)
        return

    # Start Conversation
    try:
        async with client.conversation(chat_id, timeout=120) as conv:
            lib_msg = await conv.send_message(result.message, buttons=result.keyboard)

            # 等待库选择
            press_event = await conv.wait_event(
                events.CallbackQuery(func=lambda e: e.message_id == lib_msg.id)
            )

            data = press_event.data.decode('utf-8')
            if data == 'req_cancel':
                await press_event.answer("已取消")
                await press_event.delete()
                return

            # 解析库选择
            # 预期：req_lib_{lib_b64}_{user_id}
            if not data.startswith('req_lib_'):
                await press_event.answer("无效选择")
                return

            parts = data.split('_')
            # req, lib, b64, userid
            lib_b64 = parts[2]
            library_name = base64.b64decode(lib_b64).decode('utf-8')

            await press_event.answer(f"已选择: {library_name}")



            cancel_button = [Button.inline("取消", b"req_conv_cancel_query")]
            query_prompt = await press_event.edit(
                textwrap.dedent(f"""
                已选择媒体库: **{library_name}**
                
                请发送您想搜索的关键词，支持：
                1. 标题: 例如 `流浪地球`
                2. ID: 例如 `tvdb:430047` 或 `tmdb:842675`
                """),
                buttons=cancel_button
            )

            query = await get_user_input_or_cancel(conv, query_prompt.id)
            if not query:
                try:
                    await query_prompt.delete()
                except: 
                    pass
                return

            searching_msg = await conv.send_message(f"🔍 正在搜索: **{query}**...")
            search_result = await request_service.search_media(library_name, query)

            if not search_result.success:
                await searching_msg.edit(f"❌ 搜索失败: {search_result.message}")
                return

            results_msg = await searching_msg.edit(search_result.message, buttons=search_result.keyboard)

            sel_event = await conv.wait_event(
                 events.CallbackQuery(func=lambda e: e.message_id == results_msg.id)
            )

            sel_data = sel_event.data.decode('utf-8')
            if sel_data == 'req_cancel':
                await sel_event.answer("已取消")
                await sel_event.delete()
                return

            # 解析选择: req_sel_{lib_b64}_{media_id}
            sel_parts = sel_data.split('_')
            media_id = int(sel_parts[3])

            await sel_event.answer("获取详情中...", alert=False)
            preview_result = await request_service.process_media_selection(user_id, library_name, media_id)
            if not preview_result.success:
                await sel_event.answer(preview_result.message, alert=True)
                return

            # 显示预览卡片
            preview_msg = await sel_event.edit(
                preview_result.message,
                file=preview_result.extra_data,
                buttons=preview_result.keyboard
            )

            # 9. 等待最终确认
            confirm_event = await conv.wait_event(
                events.CallbackQuery(func=lambda e: e.message_id == preview_msg.id)
            )

            confirm_data = confirm_event.data.decode('utf-8')
            if confirm_data == 'req_cancel':
                await confirm_event.answer("已取消")
                await confirm_event.delete()
                return

            if confirm_data.startswith('req_submit_'):
                await confirm_event.answer("正在提交...", alert=False)
                final_result = await request_service.submit_final_request(user_id, library_name, media_id)

                if final_result.success:
                    await confirm_event.edit(final_result.message, buttons=None, file=None)
                    return
                else:
                    await confirm_event.answer(final_result.message + "，请重试！", alert=True)

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或点击之前的【取消】按钮，或发送 /cancel 指令。", alert=True)
    except asyncio.TimeoutError:
        await safe_respond(event, "⏳ 操作超时，请重试。")
    except Exception as e:
        logger.error(f"Conversation error: {e}")
        await safe_respond(event, f"发生错误: {str(e)}")

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'^me_subtitle_(\\d+)'))
@provide_db_session
async def start_upload_sub_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """开始上传字幕处理器 (Conversation Mode)"""
    user_id = int(event.pattern_match.group(1).decode('utf-8')) # type: ignore
    chat_id = event.chat_id
    subtitle_service = SubtitleService(app, session)
    client = app.state.telethon_client.client

    if not subtitle_service.sonarr_clients and not subtitle_service.radarr_clients:
        await event.answer("系统未配置任何 Sonarr 或 Radarr 实例，无法使用此功能。", alert=True)
        return

    # Start Conversation
    try:
        async with client.conversation(chat_id, timeout=300) as conv:
            await event.answer()

            # 直接发送指令
            intro_msg = textwrap.dedent("""
                📤 **上传字幕**
                请直接发送字幕压缩包 (Zip)。
                
                **🗂 命名规则 (必须严格遵守)**：
                • **剧集**: `tvdb-ID.zip` (例如 `tvdb-430047.zip`)
                • **电影**: `tmdb-ID.zip` (例如 `tmdb-842675.zip`)
                
                **📄 压缩包内文件要求**：
                • **剧集**: S季E集.字幕语言.后缀
                • **电影**: 电影名.字幕语言.后缀

                **建议**：
                添加字幕所属字幕组或来源，命名规范：
                S季E集或电影名.字幕语言.字幕或来源.后缀

                发送 `/cancel` 或其它指令可退出上传模式。
                """)

            # 使用新消息以避免编辑可能旧的菜单消息
            await conv.send_message(intro_msg)

            # Wait for file
            while True:
                response_msg = await conv.get_response()
                if response_msg.text and response_msg.text.startswith('/'):
                    # 用户可能正在尝试运行命令，取消对话
                    await conv.send_message("❌ 检测到命令，已取消上传。")
                    return

                if not response_msg.file:
                    await conv.send_message("请发送一个带有文件的消息 (Zip 格式)，或发送 /cancel 取消。")
                    continue

                if not response_msg.file.name.lower().endswith('.zip'):
                    await conv.send_message("❌ 格式错误！仅支持 `.zip` 格式的压缩包，请重新发送。")
                    continue

                # Valid file found
                break

            processing_msg = await conv.send_message("📥 正在接收并处理文件，请稍候...")

            # Download
            async with aiofiles.tempfile.NamedTemporaryFile(suffix=".zip") as tmp_file:
                file_path = await response_msg.download_media(file=tmp_file.name)

                if not file_path:
                    await processing_msg.edit("❌ 文件下载失败，请重试。")
                    return

                # Process
                result = await subtitle_service.handle_file_upload(user_id, file_path, response_msg.file.name)
                if result.success:
                    await processing_msg.edit(result.message)
                    return
                else:
                    await processing_msg.edit(f"❌ **上传失败**\n\n{result.message}")

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或点击之前的【取消】按钮，或发送 /cancel 指令。", alert=True)
    except asyncio.TimeoutError:
        await safe_respond(event, "⏳ 操作超时，字幕上传会话已结束。")
    except Exception as e:
        logger.error(f"Conversation error: {e}")
        await safe_respond(event, f"❌ 发生未知错误: {str(e)}")

@TelethonClientWarper.handler(events.CallbackQuery(data=b'add_server_flow'))
@provide_db_session
@require_admin
async def add_server_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """添加服务器向导 (Conversation)"""
    chat_id = event.chat_id
    client = app.state.telethon_client.client
    settings_service = SettingsServices(app, session)

    try:
        async with client.conversation(chat_id, timeout=120) as conv:
            # 1. 选择服务器类型
            keyboard = [
                [
                    Button.inline("Emby", b"add_srv_type_emby"),
                    Button.inline("Jellyfin", b"add_srv_type_jellyfin")
                ],
                [
                    Button.inline("Sonarr", b"add_srv_type_sonarr"),
                    Button.inline("Radarr", b"add_srv_type_radarr")
                ],
                [Button.inline("取消", b"add_srv_cancel")]
            ]
            type_msg = await conv.send_message("🛠 **步骤 1/4**: 请选择服务器类型：", buttons=keyboard)

            # 等待类型选择
            press = await conv.wait_event(events.CallbackQuery())
            data = press.data.decode()

            if data == 'add_srv_cancel':
                await press.answer("已取消")
                await press.delete()
                await type_msg.delete()
                return

            if not data.startswith('add_srv_type_'):
                # 防止意外捕获其他按钮，简单处理退出
                await press.answer("操作无效")
                return

            server_type = data.split('_')[-1] # emby, jellyfin, sonarr, radarr
            await press.answer(f"已选择: {server_type}")
            await type_msg.delete()

            # 2. 输入名称
            cancel_btn = [Button.inline("取消", b"add_srv_abort")]
            prompt_name = await conv.send_message(f"🛠 **步骤 2/4**: 请输入 **{server_type}** 的名称 (唯一标识)：", buttons=cancel_btn)
            name = await get_user_input_or_cancel(conv, prompt_name.id)
            if not name:
                try:
                    await prompt_name.delete()
                except:
                    pass
                return
            try:
                await prompt_name.delete()
            except:
                pass

            # 3. 输入 URL
            prompt_url = await conv.send_message(
                "🛠 **步骤 3/4**: 请输入服务器地址 (URL)\n"
                "例如: `http://192.168.1.5:8096` 或 `https://emby.domain.com`", 
                buttons=cancel_btn
            )
            url = await get_user_input_or_cancel(conv, prompt_url.id)
            if not url:
                try:
                    await prompt_url.delete()
                except:
                    pass
                return
            try:
                await prompt_url.delete()
            except:
                pass

            # 4. 输入 API Key
            prompt_key = await conv.send_message("🛠 **步骤 4/4**: 请输入 API Key：", buttons=cancel_btn)
            api_key = await get_user_input_or_cancel(conv, prompt_key.id)
            if not api_key:
                try:
                    await prompt_key.delete()
                except:
                    pass
                return
            try:
                await prompt_key.delete()
            except:
                pass

            # 5. 执行添加
            processing = await conv.send_message("⏳ 正在测试连接并保存配置...")
            result = await settings_service.add_server(name, server_type, url, api_key)

            if result.success:
                await processing.edit(result.message)
                try:
                    panel = await settings_service.get_media_panel()
                    if event.message: # type: ignore
                        await event.edit(panel.message, buttons=panel.keyboard)
                except Exception:
                    pass
            else:
                await processing.edit(f"❌ 添加失败: {result.message}，请重试！")
            return

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或点击之前的【取消】按钮，或发送 /cancel 指令。", alert=True)
    except asyncio.TimeoutError:
        await event.answer("⏳ 操作超时", alert=True)
    except Exception as e:
        logger.error(f"Add server error: {e}")
        await safe_respond(event, f"发生系统错误: {str(e)}")
