import textwrap
from typing import Any

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import events
from telethon.tl.types import KeyboardButtonWebView

from bot.decorators import provide_db_session, require_admin
from bot.utils import safe_reply, safe_respond
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from services.score_service import ScoreService
from services.user_service import UserService
from services.verification_service import VerificationService

settings = get_settings()


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
        return

    user_service = UserService(app, session)

    user_id = (await event.get_reply_message()).sender_id
    result = await user_service.get_user_info(user_id)

    await safe_respond(event, result.message)

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
    web_app_url = f"{settings.telegram_webapp_url}/webapp/settings.html"

    await event.respond(
        "🔧 **系统设置**\n\n点击下方按钮打开控制面板：",
        buttons=[
            [KeyboardButtonWebView(text="🛠 打开设置面板", url=web_app_url)]
        ]
    )
    logger.info("管理员 {} 请求设置面板", event.sender_id)
