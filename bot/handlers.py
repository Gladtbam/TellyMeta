import asyncio
import logging
import textwrap
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, errors, events

from bot.decorators import provide_db_session, require_admin
from bot.utils import safe_reply, safe_respond, safe_respond_keyboard
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService
from services.account_service import AccountService
from services.score_service import MessageTrackingState, ScoreService
from services.user_service import UserService, Result
from services.verification_service import VerificationService

logger = logging.getLogger(__name__)
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
        logger.warning("Flood wait error: waiting for %d seconds", e.seconds)
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
    user_service = UserService(session, app.state.media_client)

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

@TelethonClientWarper.handler(events.CallbackQuery(data=b'me_line'))
async def line_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """线路查询处理器
    发送当前媒体服务器的访问线路
    """
    lines = settings.media_server_url.split(':')
    if len(lines) == 2:
        if lines[0].startswith('https'):
            line = settings.media_server_url + ':443'
        else:
            line = settings.media_server_url + ':80'
    else:
        line = settings.media_server_url

    await safe_respond(event, f"当前媒体服务器访问线路: `{line}`")

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

    user_id = event.sender_id

    user_service = UserService(session, app.state.media_client)
    result = await user_service.perform_checkin(user_id)

    await safe_reply(event, result.message)

    if result.private_message:
        client: TelethonClientWarper = app.state.telethon_client
        await client.send_message(user_id, result.private_message)

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

    user_service = UserService(session, app.state.media_client)
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

    user_service = UserService(session, app.state.media_client)
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
        user = await client.client.get_entity(user_id)
        username = user.username if user.username else user_id # type: ignore
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

    user_service = UserService(session, app.state.media_client)
    result = await user_service.delete_account(target_user_id, include_telegram=True)

    await safe_reply(event, result.message)

@TelethonClientWarper.handler(events.ChatAction())
@provide_db_session
async def user_join_handler(app: FastAPI, event: events.ChatAction.Event, session: AsyncSession) -> None:
    """群组成员变动处理器
    处理新成员加入群组的事件
    """
    user_id: Any = event.user_id
    if user_id == (await app.state.telethon_client.client.get_me()).id or user_id in app.state.admin_ids or user_id is None:
        return

    if event.user_joined or event.user_added:
        verification_service = VerificationService(app, session)
        result = await verification_service.start_verification(user_id)

        await safe_respond_keyboard(event, result.message, result.keyboard, 300)

    if event.user_left or event.user_kicked:
        user_service = UserService(session, app.state.media_client)
        await user_service.delete_account(user_id, include_telegram=True)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'verify_(\\d+)'))
@provide_db_session
async def verify_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """验证码处理器
    处理用户点击验证码按钮的事件
    """
    user_id: Any = event.sender_id
    answer = event.pattern_match.group(1).decode('utf-8') # type: ignore

    verification_service = VerificationService(app, session)
    result = await verification_service.process_verifocation_attempt(user_id, answer)

    await safe_respond(event, result.message)

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
    pattern=fr'^/(.+)({settings.telegram_bot_name})?$',
    incoming=True
    ))
async def unknown_command_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """未知命令处理器
    处理未知命令，提示用户使用 /help 获取帮助
    删除所有命令消息
    """
    known_commands = [
        'start', 'help', 'me', 'info', 'line', 'chat_id',
        'checkin', 'warn', 'change', 'settle', 'signup'
    ]
    command = event.pattern_match.group(1).lower() # type: ignore
    if command not in known_commands:
        await safe_reply(event, f"未知命令: /{command}. 使用 /help 获取帮助。")
    try:
        await asyncio.sleep(1)
        await event.delete()
    except errors.FloodWaitError as e:
        logger.warning("删除消息时等待错误：等待%d秒", e.seconds)
        await asyncio.sleep(e.seconds)
        await event.delete()

@TelethonClientWarper.handler(events.NewMessage(
    pattern=fr'^/signup({settings.telegram_bot_name})?(\s.*)?$',
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

    user_id = event.sender_id
    args_str = (event.pattern_match.group(1) or "").strip() # type: ignore

    registration_service = AccountService(session, app.state.media_client)
    client: TelethonClientWarper = app.state.telethon_client
    if user_id in app.state.admin_ids and args_str:
        message = await registration_service.set_registration_mode(args_str)
        sent_msg = await client.send_message(settings.telegram_chat_id, message.message)
        if message.success:
            await client.client.pin_message(settings.telegram_chat_id, sent_msg, notify=True)
    else:
        user_entity = await client.client.get_entity(user_id)
        result = await registration_service.register(user_id, user_entity.username) # type: ignore
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

    args_str = (event.pattern_match.group(1) or "").strip() # type: ignore

    if not args_str:
        await safe_reply(event, "请在命令后添加激活码，例如: /code YOUR_CODE")
        return
    
    user_id = event.sender_id
    client: TelethonClientWarper = app.state.telethon_client
    user_entity = await client.client.get_entity(user_id)

    registration_service = AccountService(session, app.state.media_client)
    result = await registration_service.redeem_code(user_id, user_entity.username, args_str) # type: ignore
    await safe_respond(event, result.message)

@TelethonClientWarper.handler(events.CallbackQuery(data=b'me_create_code'))
@provide_db_session
async def create_code_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """生成码处理器
    处理用户点击生成码按钮的事件
    """
    telegram_repo = TelegramRepository(session)
    score = await telegram_repo.get_renew_score()
    keyboard = [
        [Button.inline("续期码 (30天)", b'create_renew')],
        [Button.inline("注册码", b'create_signup')]
    ]

    await safe_respond_keyboard(event, textwrap.dedent(f"""\
        生成码需要消耗 **{score}** 积分。
        请选择要生成的码类型：
        - 续期码：用于续期现有账户，续期30天。
        - 注册码：用于注册新账户。
        """), keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'create_(renew|signup)'))
@provide_db_session
async def create_code_confirm_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """生成码处理器
    处理用户点击生成码按钮的事件
    """
    user_id: Any = event.sender_id
    code_type = event.pattern_match.group(1).decode('utf-8') # type: ignore

    registration_service = AccountService(session, app.state.media_client)
    result = await registration_service.generate_code(user_id, code_type)

    await safe_respond(event, result.message)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'me_(renew|nfsw|forget_password)'))
@provide_db_session
async def nfsw_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """续期/NFSW/忘记密码处理器
    处理用户点击续期/NFSW/忘记密码按钮的事件"""
    user_id: Any = event.sender_id
    action = event.pattern_match.group(1).decode('utf-8') # type: ignore

    account_service = AccountService(session, app.state.media_client)
    if action == 'renew':
        result = await account_service.renew(user_id, True)
    elif action == 'nfsw':
        result = await account_service.toggle_nsfw_policy(user_id)
    elif action == 'forget_password':
        result = await account_service.forget_password(user_id)
    else:
        result  = Result(False, "未知操作。")

    await safe_respond(event, result.message)

