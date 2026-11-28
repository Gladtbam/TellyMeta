from typing import Any

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, events
from telethon.tl.types import InlineButtonTypeWebView, KeyboardInlineButton

from bot.decorators import provide_db_session, require_admin, require_real_reply
from bot.utils import safe_reply, safe_reply_keyboard, safe_respond
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from core.telegram_message import TelegramMessage
from models.events import (
    ROUTE_CATEGORY_LABELS,
    NotificationRouteCategory,
    NotificationRouteScope,
    NotificationTargetType,
    get_server_route_category_label,
)
from repositories.config_repo import ConfigRepository
from repositories.notification_repo import NotificationRouteRepository
from repositories.server_repo import ServerRepository
from services.notification_service import NotificationService
from services.score_service import ScoreService, build_score_settlement_payload
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
        web_app_buttons = [
            [
                KeyboardInlineButton(
                    text="👤 查看用户卡片",
                    type=InlineButtonTypeWebView(url=card_url),
                )
            ]
        ]

        # 私聊中支持直接附带内联 WebView 按钮
        if event.is_private:
            await safe_reply_keyboard(
                event,
                TelegramMessage.render_markdown(t"""\
                    📋 **用户资料卡片**

                    目标用户: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)
                    点击下方按钮在弹窗中查看详细信息："""),
                web_app_buttons,
                delete_after=120,
            )
            return

        # 群组中通过主 Mini App startapp 深链按钮直接在群内弹出 Mini App（需在 @BotFather 配置 Main Mini App）
        clean_bot_name = settings.telegram_bot_name.lstrip("@")
        if clean_bot_name:
            startapp_url = (
                f"https://t.me/{clean_bot_name}?startapp=user_info_{target_user_id}"
            )
            group_buttons = [[Button.url("👤 查看用户卡片", startapp_url)]]
            await safe_reply_keyboard(
                event,
                TelegramMessage.render_markdown(t"""\
                    📋 **用户资料卡片**

                    目标用户: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)
                    点击下方按钮在群内打开用户卡片："""),
                group_buttons,
                delete_after=60,
            )
            return

        # 未配置机器人用户名时，通过统一管线降级发送到管理员私聊
        dm_ok, dm_err = await NotificationService(app).deliver_direct_dm(
            user_id=admin_id,
            message=TelegramMessage.render_markdown(t"""\
                📋 **用户资料卡片**

                目标用户: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)
                请点击下方按钮打开卡片并执行管理操作："""),
            parse_mode="md",
            buttons=web_app_buttons,
            client=client,
            skip_inbox_fallback=True,
        )
        if dm_ok:
            await safe_reply(
                event,
                TelegramMessage.render_markdown(
                    t"🔍 已将用户 [{target_name}](tg://user?id={target_user_id}) 的信息卡片发送至您的私聊，请查收。"
                ),
                delete_after=30,
            )
        else:
            logger.warning(
                "未能向管理员发送私聊消息（可能未私聊启动过机器人）: {}", dm_err
            )
            user_service = UserService(app, session)
            result = await user_service.get_user_info(target_user_id)
            await safe_reply(
                event,
                TelegramMessage.render_markdown(
                    t"⚠️ 无法向您发送私聊卡片（请先私聊机器人发送 /start）。已降级为群内文本展示：\n\n{result.message:raw}"
                ),
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
    operator_name = await user_service.resolve_user_display_name(
        getattr(event, "sender_id", None), fallback="管理员"
    )
    user = await user_service.warn_user(target_user_id, operator_name=operator_name)

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
    user_service = UserService(app, session)
    if not await user_service.config_repo.is_points_enabled():
        await safe_reply(event, "❌ 全局积分总开关已关闭，无法修改积分。")
        return

    args = event.message.text.split()
    if len(args) != 2:
        await safe_reply(event, "请在命令后添加积分数，例如: /change 10 或 /change -5")
        return

    score_change = int(args[1])
    operator_name = await user_service.resolve_user_display_name(
        getattr(event, "sender_id", None), fallback="管理员"
    )
    res = await user_service.change_user_score(
        target_user_id, score_change, operator_name=operator_name
    )
    if not res.success or not res.extra_data:
        await safe_reply(event, f"❌ {res.message}")
        return
    user = res.extra_data["user"]

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
    """积分结算处理器：手动触发每日发言积分结算，受总开关与群聊发言积分子开关控制。

    Args:
        app: FastAPI 应用实例，用于读取消息计数器与 Telethon 客户端。
        event: Telegram `/settle` 指令事件对象。
        session: 异步数据库会话。

    副作用：开启时结算并清空发言积分队列并发送榜单；关闭时清空暂存队列并提示关闭原因。
    """
    allowed, reason = await ConfigRepository(session).check_points_feature(
        ConfigRepository.KEY_ENABLE_POINTS_CHAT
    )
    if not allowed:
        app.state.message_tracker.message_counts.clear()
        await safe_reply(event, reason)
        return

    score_service = ScoreService(session, app.state.message_tracker)
    result = await score_service.settle_and_clear_scores()
    client: TelethonClientWarper | None = getattr(app.state, "telethon_client", None)

    if result is None:
        await safe_reply(event, "当前无积分可结算。")
        return

    payload = await build_score_settlement_payload(
        score_service,
        client,
        result,
        is_manual=True,
    )
    await NotificationService(app).emit(
        payload,
        source_key="score_settlement:manual",
        session=session,
    )
    await safe_reply(
        event,
        f"✅ 积分结算完成，共结算 **{result.total_score_settled}** 活跃度积分，结算报告已按通知路由投递。",
    )


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
    operator_name = await user_service.resolve_user_display_name(
        getattr(event, "sender_id", None), fallback="管理员"
    )
    result = await user_service.delete_account(
        target_user_id,
        "both",
        operator_name=operator_name,
        emit_notification=True,
    )

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
        operator_name = await user_service.resolve_user_display_name(
            getattr(event, "sender_id", None), fallback="管理员"
        )
        result = await user_service.kick_and_cleanup_user(
            target_user_id,
            client=client,
            operator_name=operator_name,
            suppress_kick_error=False,
        )
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
        user_service = UserService(app, session)
        operator_name = await user_service.resolve_user_display_name(
            getattr(event, "sender_id", None), fallback="管理员"
        )
        user_name = await client.get_user_name(target_user_id)
        await client.ban_user(target_user_id)
        await user_service.notify_admin_ban_or_kick(
            action_type="ban",
            target_user_id=target_user_id,
            operator_name=operator_name,
            target_name=user_name if isinstance(user_name, str) else "",
        )
        await safe_reply(
            event,
            TelegramMessage.render_markdown(
                t"已封禁用户[{user_name}](tg://user?id={target_user_id})"
            ),
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


_BIND_GLOBAL_CATEGORIES = "account_lifecycle|ops_alert|community_score|community_verification|community_leave_audit"


@TelethonClientWarper.handler(
    events.NewMessage(
        pattern=(
            rf"^/bind(@[A-Za-z0-9_]+)?"
            rf"\s+(?:(\d+)\s+(notify|request|ops|lifecycle)|global\s+({_BIND_GLOBAL_CATEGORIES}))$"
        ),
        incoming=True,
    )
)
@provide_db_session
@require_admin
async def bind_handler(
    app: FastAPI, event: events.NewMessage.Event, session: AsyncSession
) -> None:
    """话题绑定处理器：支持单服通知话题绑定与全局通知类别话题绑定

    支持格式：
    - ``/bind <server_id> <notify|request|ops|lifecycle>``：写入单服通知路由（``notify``/``request`` 同步更新遗留话题字段）
    - ``/bind global <account_lifecycle|ops_alert|community_score|community_verification|community_leave_audit>``：
      写入全局话题路由并保留既有高危抄送与可选事件开关
    """
    match = getattr(event, "pattern_match", None)
    if match is None:
        return

    # 支持可选 @botname 后缀，明确指向其他 Bot 时静默忽略
    target_bot = match.group(1) or ""
    clean_target = target_bot.lstrip("@").lower()
    my_bot_name = (getattr(settings, "telegram_bot_name", "") or "").lstrip("@").lower()
    if clean_target and my_bot_name and clean_target != my_bot_name:
        return

    reply_to = getattr(getattr(event, "message", None), "reply_to", None)
    topic_id: int | None = None
    if reply_to and getattr(reply_to, "forum_topic", False):
        # 在话题内直接发指令时 topic_id 在 reply_to_msg_id；回复他人消息时在 reply_to_top_id
        raw_topic = getattr(reply_to, "reply_to_top_id", None) or getattr(
            reply_to, "reply_to_msg_id", None
        )
        if (
            isinstance(raw_topic, int)
            and not isinstance(raw_topic, bool)
            and raw_topic > 0
        ):
            topic_id = raw_topic

    if topic_id is None:
        await safe_reply(
            event, "❌ 请在论坛的具体**话题（Topic）内部**发送此绑定命令。"
        )
        return

    raw_chat_id = getattr(event, "chat_id", None)
    if raw_chat_id is None:
        raw_chat_id = getattr(getattr(event, "message", None), "chat_id", None)
    target_chat_id = (
        raw_chat_id
        if isinstance(raw_chat_id, int)
        and not isinstance(raw_chat_id, bool)
        and raw_chat_id != 0
        else None
    )

    route_repo = NotificationRouteRepository(session)
    groups = match.groups()
    global_cat_raw = groups[3] if len(groups) >= 4 else None
    if global_cat_raw:
        category = NotificationRouteCategory(global_cat_raw)
        existing_global = await route_repo.get_route(
            scope_type=NotificationRouteScope.GLOBAL,
            category=category,
            fresh=True,
        )
        await route_repo.upsert_route(
            scope_type=NotificationRouteScope.GLOBAL,
            server_id=None,
            category=category,
            target_type=NotificationTargetType.FORUM_TOPIC,
            target_chat_id=target_chat_id,
            target_topic_id=topic_id,
            escalate_admin_dm=(
                bool(existing_global.escalate_admin_dm) if existing_global else False
            ),
            enabled_events_json=(
                existing_global.enabled_events_json if existing_global else None
            ),
        )
        cat_label = ROUTE_CATEGORY_LABELS.get(category, (category.value, ""))[0]
        await safe_reply(
            event,
            TelegramMessage.render_markdown(
                t"✅ 成功将全局 **{cat_label}** (`{category.value}`) 绑定到当前话题！(ID: `{topic_id}`)"
            ),
        )
        return

    server_id = int(match.group(2))
    bind_type = match.group(3)

    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if not server:
        await safe_reply(event, "❌ 找不到对应的服务器。")
        return

    if bind_type == "notify":
        await server_repo.update_notify_config(server_id, notify_topic_id=topic_id)
        category = NotificationRouteCategory.MEDIA_LIBRARY
    elif bind_type == "request":
        await server_repo.update_notify_config(
            server_id, request_notify_topic_id=topic_id
        )
        category = NotificationRouteCategory.MEDIA_REQUEST
    elif bind_type == "ops":
        category = NotificationRouteCategory.OPS_ALERT
    elif bind_type == "lifecycle":
        category = NotificationRouteCategory.ACCOUNT_LIFECYCLE
    else:
        await safe_reply(
            event, "❌ 无效的绑定类型，请使用 notify、request、ops 或 lifecycle。"
        )
        return
    msg_type = get_server_route_category_label(server.server_type, category)[0]

    existing_srv_route = await route_repo.get_route(
        scope_type=NotificationRouteScope.SERVER,
        server_id=server_id,
        category=category,
        fresh=True,
    )
    await route_repo.upsert_route(
        scope_type=NotificationRouteScope.SERVER,
        server_id=server_id,
        category=category,
        target_type=NotificationTargetType.FORUM_TOPIC,
        target_chat_id=target_chat_id,
        target_topic_id=topic_id,
        escalate_admin_dm=(
            bool(existing_srv_route.escalate_admin_dm) if existing_srv_route else False
        ),
        enabled_events_json=(
            existing_srv_route.enabled_events_json if existing_srv_route else None
        ),
    )

    await safe_reply(
        event,
        TelegramMessage.render_markdown(
            t"✅ 成功将 **{server.name}** 的 **{msg_type}** 绑定到当前话题！(ID: `{topic_id}`)"
        ),
    )
