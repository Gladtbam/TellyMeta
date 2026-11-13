import asyncio
import contextlib
import textwrap
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import errors, events

from bot.decorators import provide_db_session
from bot.utils import safe_delete, safe_reply, safe_respond, safe_respond_keyboard
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from services.score_service import MessageTrackingState, ScoreService
from services.user_service import UserService
from services.verification_service import VerificationService

settings = get_settings()

# 定义不需要计数的关键词
IGNORED_KEYWORDS = ["冒泡", "冒个泡", "好", "签到", "观看度"]


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=rf"^/start({settings.telegram_bot_name})?$", incoming=True
    )
)
@provide_db_session
async def start_handler(
    app: FastAPI, event: events.NewMessage.Event, session: AsyncSession
) -> None:
    """欢迎消息处理器"""
    verification_service = VerificationService(app, session)
    challenge_data = await verification_service.create_get_challenge_details(
        event.sender_id
    )
    if challenge_data is None:
        await help_handler(app, event)  # 发送帮助消息
        return
    image_data, keyboard = challenge_data
    try:
        await event.respond(
            "欢迎！请在 **5 分钟内**选择下方正确答案：",
            file=image_data,
            buttons=keyboard,
        )
    except errors.FloodWaitError as e:
        logger.warning("等待错误：等待 {} 秒", e.seconds)
        await asyncio.sleep(e.seconds)
        await event.respond(
            "欢迎！请在 **5 分钟内**选择下方正确答案：",
            file=image_data,
            buttons=keyboard,
        )


@TelethonClientWarper.handler(
    events.NewMessage(pattern=rf"^/help({settings.telegram_bot_name})?$", incoming=True)
)
async def help_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """帮助消息处理器"""
    msg = textwrap.dedent("""\
    /help - [私聊]帮助
    /checkin - 签到
    /del - [管理员]删除 Emby 账户, 需回复一个用户
    /warn - [管理员]警告用户, 需回复一个用户
    /info - [管理员]查看用户信息
    /settle - [管理员]手动结算积分
    /change - [管理员]手动修改积分, 正数加负数减
    /kick - [管理员]踢出用户, 需回复一个用户
    /ban - [管理员]封禁用户, 需回复一个用户
    /chat_id - 获取当前群组ID, 需在群组中使用
    """)

    if event.is_private:
        await safe_respond(event, msg)
    else:
        await safe_reply(event, f"私聊我获取帮助: {settings.telegram_bot_name}", 20)


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=rf"^/chat_id({settings.telegram_bot_name})?$", incoming=True
    )
)
async def chat_id_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """群组ID处理器
    发送当前群组的ID，需在群组中使用
    """
    if event.is_private:
        await safe_reply(event, "请在群组中使用此命令以获取群组ID。")
    else:
        await safe_reply(event, f"当前群组ID: `{event.chat_id}`")


async def _get_bot_self_id(tg_client: TelethonClientWarper) -> int | None:
    """获取并缓存 Bot 自身的 Telegram 用户 ID，避免每次入/退群重复发起 RPC。"""
    cached = getattr(tg_client, "_cached_me_id", None)
    if cached is not None:
        return cached
    me = await tg_client.client.get_me()
    me_id = getattr(me, "id", None)
    if me_id is not None:
        setattr(tg_client, "_cached_me_id", me_id)
    return me_id


def _extract_action_user_ids(event: events.ChatAction.Event) -> list[int]:
    """从群成员变动事件中提取去重后的目标用户 ID 列表（兼容批量拉人）。"""
    users = getattr(event, "users", None) or []
    ids: list[int] = []
    for u in users:
        uid = getattr(u, "id", None)
        if isinstance(uid, int) and uid not in ids:
            ids.append(uid)
    if not ids and isinstance(event.user_id, int):
        ids.append(event.user_id)
    return ids


@TelethonClientWarper.handler(events.ChatAction(chats=settings.telegram_chat_id))
@provide_db_session
async def user_join_handler(
    app: FastAPI, event: events.ChatAction.Event, session: AsyncSession
) -> None:
    """群组成员变动处理器：处理新成员入群验证与成员退群/被踢清理。"""
    target_ids = _extract_action_user_ids(event)
    if not target_ids:
        return

    me_id = await _get_bot_self_id(app.state.telethon_client)
    admin_ids = app.state.admin_ids
    effective_ids = [uid for uid in target_ids if uid != me_id and uid not in admin_ids]
    if not effective_ids:
        return

    verification_enabled = (
        ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_VERIFICATION, "true")
        == "true"
    )

    if (event.user_joined or event.user_added) and verification_enabled:
        added_by_id = getattr(getattr(event, "added_by", None), "id", None)
        if event.user_added and added_by_id in admin_ids:
            logger.info(
                "用户 {} 由管理员 {} 邀请，已跳过验证流程。", effective_ids, added_by_id
            )
            return

        verification_service = VerificationService(app, session)
        for user_id in effective_ids:
            logger.info("用户 {} 加入", user_id)
            result = await verification_service.start_verification(user_id)
            if not result.success:
                continue
            message = await safe_respond_keyboard(
                event, result.message, result.keyboard, 300
            )
            if message and message.id:
                await verification_service.verification_repo.update_message_id(
                    user_id, message.id
                )

    if event.user_left or event.user_kicked:
        verification_service = VerificationService(app, session)
        user_service = UserService(app, session)
        for user_id in effective_ids:
            logger.info("用户 {} 离开", user_id)
            challenge = await verification_service.verification_repo.get(user_id)
            if challenge:
                with contextlib.suppress(JobLookupError):
                    verification_service.scheduler.remove_job(
                        challenge.scheduler_job_id
                    )
                await verification_service.verification_repo.delete(user_id)

            await user_service.delete_account(user_id, "both")


@TelethonClientWarper.handler(events.CallbackQuery(pattern=b"verify_(\\d+)"))
@provide_db_session
async def verify_handler(
    app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession
) -> None:
    """验证码处理器
    处理用户点击验证码按钮的事件
    """
    user_id: Any = event.sender_id
    answer = event.pattern_match.group(1).decode("utf-8")  # type: ignore

    verification_service = VerificationService(app, session)
    client: TelethonClientWarper = app.state.telethon_client
    result = await verification_service.process_verifocation_attempt(user_id, answer)

    await safe_respond(event, result.message)
    if (
        result.success
        and result.private_message
        and isinstance(result.private_message, int)
    ):
        try:
            await client.edit_message(
                settings.telegram_chat_id,
                result.private_message,
                "您已通过验证，可以在群组中发言了。",
            )
        except (
            errors.MessageIdInvalidError,
            errors.MessageNotModifiedError,
            errors.RPCError,
        ) as e:
            logger.warning("更新群组验证欢迎消息失败: {}", e)


@TelethonClientWarper.handler(
    events.NewMessage(chats=settings.telegram_chat_id, incoming=True)
)
@provide_db_session
async def group_message_handler(
    app: FastAPI, event: events.NewMessage.Event, session: AsyncSession
) -> None:
    """群组消息处理器
    根据消息类型，计算积分，连续发送不计入积分
    """
    if not event.sender_id or not event.message or not event.message.text:
        return  # 忽略无发送者或无文本的消息

    if getattr(event, "out", False) or getattr(
        getattr(event, "sender", None), "bot", False
    ):
        return  # 忽略 Bot 自身或其它 Bot 发出的消息

    user_id = event.sender_id

    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_POINTS, "true") != "true":
        return

    text = event.message.text.strip()
    if text.startswith("/"):
        return

    # 无意义水贴关键词仅不计积分，仍纳入防刷屏连续计数
    count_score = text not in IGNORED_KEYWORDS

    flood_state: MessageTrackingState = app.state.message_tracker
    antiflood_service = ScoreService(session, flood_state)
    flood_result = await antiflood_service.process_message(
        user_id, app.state.admin_ids, count_score=count_score
    )
    if flood_result:
        await safe_reply(event, flood_result.message)


@TelethonClientWarper.handler(
    events.NewMessage(pattern=r"^/(\w+)(?:@(\w+))?$", incoming=True)
)
async def unknown_command_handler(app: FastAPI, event: events.NewMessage.Event) -> None:
    """未知命令处理器
    忽略 @ 其它 Bot 的指令；对其余未知命令提示 /help 并安全清理消息
    """
    known_commands = {
        "start",
        "help",
        "info",
        "chat_id",
        "del",
        "checkin",
        "warn",
        "change",
        "settle",
        "kick",
        "ban",
        "bind",
        "cancel",
    }
    try:
        command = event.pattern_match.group(1).lower()  # type: ignore
        target_bot = event.pattern_match.group(2)  # type: ignore
    except IndexError:
        logger.warning("unknown_command_handler 正则分组缺失")
        return

    # 明确 @ 了其他 Bot 时直接忽略，不回复也不尝试删除
    my_bot_name = (settings.telegram_bot_name or "").lstrip("@").lower()
    if target_bot and my_bot_name and target_bot.lower() != my_bot_name:
        return

    if command not in known_commands:
        await safe_reply(event, f"未知命令: /{command}. 使用 /help 获取帮助。")

    await asyncio.sleep(1)
    await safe_delete(event.message)
