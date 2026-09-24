import textwrap
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger
from telethon import Button, events
from telethon.tl.types import KeyboardButtonWebView

from bot.decorators import provide_db_session, require_admin, require_real_reply
from bot.utils import safe_reply, safe_reply_keyboard, safe_respond
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.server_repo import ServerRepository
from services.score_service import ScoreService
from services.user_service import UserService
from services.verification_service import VerificationService

settings = get_settings()


@TelethonClientWarper.handler(
    events.NewMessage(pattern=rf"^/info({settings.telegram_bot_name})?$", incoming=True)
)
@provide_db_session
@require_admin
@require_real_reply
async def info_handler(
    app: FastAPI,
    event: events.NewMessage.Event,
    session: AsyncSession,
    target_user_id: int,
) -> None:
    """用户信息处理器
    发送用户信息，需回复一个用户
    """
    admin_id = event.sender_id
    client: TelethonClientWarper = app.state.telethon_client
    target_name = await client.get_user_name(target_user_id) or str(target_user_id)

    if settings.telegram_webapp_url:
        webapp_base = settings.telegram_webapp_url.rstrip("/")
        card_url = f"{webapp_base}/webapp/user_info.html?user_id={target_user_id}"
        web_app_buttons = [[KeyboardButtonWebView(text="👤 查看用户卡片", url=card_url)]]

        # 如果当前就是在私聊中，Telegram 允许直接发送 KeyboardButtonWebView 按钮
        if event.is_private:
            await safe_reply_keyboard(
                event,
                f"📋 **用户资料卡片**\n\n"
                f"目标用户: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                f"点击下方按钮在弹窗中查看详细信息：",
                web_app_buttons,
                delete_after=120,
            )
            return

        # 如果在群组中，由于 Telegram API 限制群聊消息不能附带 KeyboardButtonWebView，将卡片发送到管理员私聊
        try:
            await client.send_message(
                admin_id,
                f"📋 **用户资料卡片**\n\n"
                f"目标用户: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                f"请点击下方按钮打开卡片并执行管理操作：",
                buttons=web_app_buttons,
            )

            clean_bot_name = settings.telegram_bot_name.lstrip("@")
            group_buttons = (
                [[Button.url("💬 前往私聊查看", f"https://t.me/{clean_bot_name}")]]
                if clean_bot_name
                else None
            )

            msg = f"🔍 已将用户 [{target_name}](tg://user?id={target_user_id}) 的信息卡片发送至您的私聊，请查收。"
            if group_buttons:
                await safe_reply_keyboard(event, msg, group_buttons, delete_after=30)
            else:
                await safe_reply(event, msg, delete_after=30)

        except Exception as e:
            logger.warning("未能向管理员发送私聊消息（可能未私聊启动过机器人）: {}", e)
            user_service = UserService(app, session)
            result = await user_service.get_user_info(target_user_id)
            await safe_reply(
                event,
                f"⚠️ 无法向您发送私聊卡片（请先私聊机器人发送 /start）。已降级为群内文本展示：\n\n{result.message}",
                delete_after=60,
            )
    else:
        user_service = UserService(app, session)
        result = await user_service.get_user_info(target_user_id)
        await safe_respond(event, result.message)


@TelethonClientWarper.handler(
    events.NewMessage(pattern=rf"^/warn({settings.telegram_bot_name})?$", incoming=True)
)
@provide_db_session
@require_admin
@require_real_reply
async def warn_handler(
    app: FastAPI,
    event: events.NewMessage.Event,
    session: AsyncSession,
    target_user_id: int,
) -> None:
    """警告处理器
    警告一个用户，需回复一个用户
    """
    user_service = UserService(app, session)
    user = await user_service.telegram_repo.update_warn_and_score(target_user_id)

    await safe_reply(
        event,
        f"✅ 用户 [{user.id}](tg://user?id={user.id}) 已被警告，当前警告次数: **{user.warning_count}**。",
    )


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=rf"^/change({settings.telegram_bot_name})?\s+(-?\d+)$", incoming=True
    )
)
@provide_db_session
@require_admin
@require_real_reply
async def change_handler(
    app: FastAPI,
    event: events.NewMessage.Event,
    session: AsyncSession,
    target_user_id: int,
) -> None:
    """修改积分处理器
    修改一个用户的积分，需回复一个用户并在命令后添加积分数
    """
    args = event.message.text.split()
    if len(args) != 2:
        await safe_reply(event, "请在命令后添加积分数，例如: /change 10 或 /change -5")
        return

    score_change = int(args[1])
    user_service = UserService(app, session)
    user = await user_service.telegram_repo.update_score(target_user_id, score_change)

    await safe_reply(
        event,
        f"✅ 用户 [{user.id}](tg://user?id={user.id}) 的积分已修改，当前积分: **{user.score}**。",
    )


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=rf"^/settle({settings.telegram_bot_name})?$", incoming=True
    )
)
@provide_db_session
@require_admin
async def settle_handler(
    app: FastAPI, event: events.NewMessage.Event, session: AsyncSession
) -> None:
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
    for user_id, score_change in result.user_score_changes.items():  # type: ignore
        username = await client.get_user_name(user_id)
        user_details.append(
            f"- [{username}](tg://user?id={user_id}): `+{score_change}`"
        )
    final_summary = summary + "\n".join(user_details)
    await client.client.edit_message(summary_msg, final_summary)


@TelethonClientWarper.handler(
    events.NewMessage(pattern=rf"^/del({settings.telegram_bot_name})?$", incoming=True)
)
@provide_db_session
@require_admin
@require_real_reply
async def delete_handler(
    app: FastAPI,
    event: events.NewMessage.Event,
    session: AsyncSession,
    target_user_id: int,
) -> None:
    """删除账户处理器
    删除一个用户的 Emby 账户，需回复一个用户
    """
    user_service = UserService(app, session)
    result = await user_service.delete_account(target_user_id, "both")

    await safe_reply(event, result.message)


@TelethonClientWarper.handler(
    events.NewMessage(pattern=rf"^/kick({settings.telegram_bot_name})?$", incoming=True)
)
@TelethonClientWarper.handler(events.CallbackQuery(pattern=b"kick_(\\d+)"))
@provide_db_session
@require_admin
@require_real_reply
async def kick_handler(
    app: FastAPI, event: Any, session: AsyncSession, target_user_id: int = 0
) -> None:
    """踢出处理器
    踢出一个用户，支持命令和按钮两种触发方式
    """

    if isinstance(event, events.NewMessage.Event):
        user_service = UserService(app, session)
        client: TelethonClientWarper = app.state.telethon_client
        await client.kick_and_ban_participant(target_user_id)
        result = await user_service.delete_account(target_user_id, "both")
        await safe_reply(event, "已踢出用户。\n" + result.message)
    elif isinstance(event, events.CallbackQuery.Event):
        target_user_id = int(event.pattern_match.group(1).decode("utf-8"))  # type: ignore
        verification_service = VerificationService(app, session)
        result = await verification_service.reject_verification(target_user_id)
        await event.edit(result.message)
    else:
        await safe_respond(event, "无法处理此事件类型。")
        return


@TelethonClientWarper.handler(
    events.NewMessage(pattern=rf"^/ban({settings.telegram_bot_name})?$", incoming=True)
)
@TelethonClientWarper.handler(events.CallbackQuery(pattern=b"ban_(\\d+)"))
@provide_db_session
@require_admin
@require_real_reply
async def ban_handler(
    app: FastAPI, event: Any, session: AsyncSession, target_user_id: int = 0
) -> None:
    """封禁处理器
    封禁一个用户，支持命令和按钮两种触发方式
    """

    if isinstance(event, events.NewMessage.Event):
        client: TelethonClientWarper = app.state.telethon_client
        user_name = await client.get_user_name(target_user_id)
        await client.ban_user(target_user_id)
        await safe_reply(
            event, f"已封禁用户[{user_name}](tg://user?id={target_user_id})"
        )
    elif isinstance(event, events.CallbackQuery.Event):
        target_user_id = int(event.pattern_match.group(1).decode("utf-8"))  # type: ignore
        verification_service = VerificationService(app, session)
        result = await verification_service.reject_verification(
            target_user_id, is_ban=True
        )
        await event.edit(result.message)
    else:
        await safe_respond(event, "无法处理此事件类型。")
        return


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=rf"^/bind({settings.telegram_bot_name})?\s+(\d+)\s+(notify|request)$",
        incoming=True,
    )
)
@provide_db_session
@require_admin
async def bind_handler(
    app: FastAPI, event: events.NewMessage.Event, session: AsyncSession
) -> None:
    """话题绑定处理器
    /bind <server_id> <notify|request>
    """
    server_id = int(event.pattern_match.group(2))  # type: ignore
    bind_type = event.pattern_match.group(3)  # type: ignore

    topic_id = None
    if getattr(event.message, "reply_to", None) and event.message.reply_to.forum_topic:
        topic_id = event.message.reply_to.reply_to_msg_id
    else:
        await safe_reply(
            event, "❌ 请在论坛的具体**话题（Topic）内部**发送此绑定命令。"
        )
        return

    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        await safe_reply(event, "❌ 找不到对应的服务器。")
        return

    msg_type = None
    if bind_type == "notify":
        await server_repo.update_notify_config(server_id, notify_topic_id=topic_id)
        msg_type = "常规通知"
    elif bind_type == "request":
        await server_repo.update_notify_config(
            server_id, request_notify_topic_id=topic_id
        )
        msg_type = "求片通知"

    await safe_reply(
        event,
        f"✅ 成功将 **{server.name}** 的 **{msg_type}** 绑定到当前话题！(ID: `{topic_id}`)",
    )
