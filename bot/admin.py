import asyncio
import base64
import textwrap
from typing import Any, cast

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, errors, events
from telethon.tl.types import KeyboardButtonWebView

from bot.decorators import provide_db_session, require_admin
from bot.utils import (get_user_input_or_cancel, safe_delete, safe_reply,
                       safe_respond, safe_respond_keyboard)
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from services.score_service import ScoreService
from services.settings_service import SettingsServices
from services.user_service import Result, UserService
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
    pattern=b'manage_(admins|media|system|main)'))
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

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_input_mode_(\\d+)_(count|time|external)'))
@provide_db_session
async def srv_input_mode_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """设置复杂模式 (名额/时间/外部)，触发对话"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    target = event.pattern_match.group(2).decode() # type: ignore

    chat_id = event.chat_id
    client = app.state.telethon_client.client

    prompt = ""
    if target == "count":
        prompt = "请输入开放注册的名额数量 (纯数字，例如 `50`)："
    elif target == "time":
        prompt = "请输入开放时长 (格式如 `1h`, `30m`, `1h30m`)："
    elif target == "external":
        prompt = textwrap.dedent("""\
            请输入外部验证链接的前缀 (包含 http/https)。
            支持多个前缀，使用 `|` 分隔。
            
            逻辑说明: 系统会将用户输入的验证码拼接到此链接后进行 GET 请求。
            1. 如果用户输入的是完整 URL 且匹配前缀，直接使用。
            2. 否则，将用户输入的 Key 拼接到第一个前缀后进行 GET 请求。
        """)

    try:
        async with client.conversation(chat_id, timeout=60) as conv:
            cancel_btn = [Button.inline("取消", b"srv_mode_cancel")]
            prompt_msg = await conv.send_message(prompt, buttons=cancel_btn)

            input_val = await get_user_input_or_cancel(conv, prompt_msg.id)

            if not input_val:
                await safe_delete(prompt_msg)
                return

            await safe_delete(prompt_msg)

            external_parser = None
            if target == "external":
                parser_prompt = textwrap.dedent("""\
                    请输入 **验证解析代码** (Python 表达式)。
                    
                    可用变量: `response` (或 `r`), `json`, `base64`, `re`, `str`, `int` 等。
                    要求: 返回布尔值 `True` (通过) 或 `False` (失败)。
                    
                    示例: `response.status_code == 200`
                    或: `json.loads(r.text)['status'] == 'ok'`
                    
                    发送 `/empty` 可跳过 (使用默认 2xx 状态码判断)。
                """)
                parser_msg = await conv.send_message(parser_prompt, buttons=cancel_btn)
                parser_input = await get_user_input_or_cancel(conv, parser_msg.id)

                if parser_input is None:
                    await safe_delete(parser_msg)
                    return

                if parser_input.strip() != "/empty":
                    external_parser = parser_input.strip()

                await safe_delete(parser_msg)

            settings_service = SettingsServices(app, session)
            result = await settings_service.set_server_registration_mode(
                server_id,
                input_val,
                external_parser)

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

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_set_notify_(\\d+)_(normal|request)'))
@provide_db_session
async def srv_set_notify_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    notify_type = event.pattern_match.group(2).decode() # type: ignore

    settings_service = SettingsServices(app, session)
    result = await settings_service.get_server_notify_topic_selection(server_id, notify_type)

    if result.success:
        await event.edit(result.message, buttons=result.keyboard)
    else:
        await event.answer(result.message, alert=True)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_save_topic_(\\d+)_(normal|request)_(-?\\d+)'))
@provide_db_session
async def srv_save_topic_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    notify_type = event.pattern_match.group(2).decode() # type: ignore
    topic_id = int(event.pattern_match.group(3).decode()) # type: ignore

    settings_service = SettingsServices(app, session)
    await settings_service.set_server_notify_topic(server_id, notify_type, topic_id)

    await event.answer("设置已保存")

    # 返回详情
    result = await settings_service.get_server_detail_panel(server_id)
    await event.edit(result.message, buttons=result.keyboard)

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
            prompt_name = await conv.send_message(
                f"🛠 **步骤 2/4**: 请输入 **{server_type}** 的名称 (唯一标识)：",
                buttons=cancel_btn
            )
            name = await get_user_input_or_cancel(conv, prompt_name.id)
            if not name:
                await safe_delete(prompt_name)
                return
            await safe_delete(prompt_name)

            # 3. 输入 URL
            prompt_url = await conv.send_message(
                "🛠 **步骤 3/4**: 请输入服务器地址 (URL)\n"
                "例如: `http://192.168.1.5:8096` 或 `https://emby.domain.com`", 
                buttons=cancel_btn
            )
            url = await get_user_input_or_cancel(conv, prompt_url.id)
            if not url:
                await safe_delete(prompt_url)
                return
            await safe_delete(prompt_url)

            # 4. 输入 API Key
            prompt_key = await conv.send_message("🛠 **步骤 4/4**: 请输入 API Key：", buttons=cancel_btn)
            api_key = await get_user_input_or_cancel(conv, prompt_key.id)
            if not api_key:
                await safe_delete(prompt_key)
                return
            await safe_delete(prompt_key)

            # 5. 执行添加
            processing = await conv.send_message("⏳ 正在测试连接并保存配置...")
            result = await settings_service.add_server(name, server_type, url, api_key)

            if result.success:
                await processing.edit(result.message)
            try:
                panel = await settings_service.get_media_panel()
                if event.message: # type: ignore
                    await event.edit(panel.message, buttons=panel.keyboard)
            except (errors.MessageNotModifiedError, errors.MessageIdInvalidError):
                pass
            except Exception as e:
                logger.warning("刷新面板失败: {}", e)
            else:
                await processing.edit(f"❌ 添加失败: {result.message}，请重试！")
            return

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或点击之前的【取消】按钮，或发送 /cancel 指令。", alert=True)
    except asyncio.TimeoutError:
        await event.answer("⏳ 操作超时", alert=True)
    except Exception as e:
        logger.error("添加服务器失败: {}", e)
        await safe_respond(event, "发生系统错误，请重试")

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_toggle_enable_(\\d+)'))
@provide_db_session
@require_admin
async def srv_toggle_enable_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """切换服务器启用/禁用状态"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    settings_service = SettingsServices(app, session)

    result = await settings_service.toggle_server_status(server_id)
    await event.answer(result.message)

    # 刷新详情面板
    detail_result = await settings_service.get_server_detail_panel(server_id)
    if detail_result.success:
        await event.edit(detail_result.message, buttons=detail_result.keyboard)

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_edit_(name|url|key|tos)_(\\d+)'))
@provide_db_session
@require_admin
async def srv_edit_field_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """编辑服务器字段 (名称/URL/APIKey)"""
    field_type = event.pattern_match.group(1).decode() # type: ignore
    server_id = int(event.pattern_match.group(2).decode()) # type: ignore

    chat_id = event.chat_id
    client = app.state.telethon_client.client
    settings_service = SettingsServices(app, session)

    field_map = {
        'name': '名称',
        'url': '地址 (URL)',
        'key': 'API Key',
        'tos': '用户协议 (TOS)'
    }
    db_field_map = {
        'name': 'name',
        'url': 'url',
        'key': 'api_key',
        'tos': 'tos'
    }
    field_name = field_map.get(field_type, field_type)
    db_field = cast(str, db_field_map.get(field_type, field_type))

    try:
        async with client.conversation(chat_id, timeout=300) as conv:
            cancel_btn = [Button.inline("取消", b"srv_edit_cancel")]
            prompt_text = f"✏️ 请输入新的 **{field_name}**："
            if field_type == 'tos':
                prompt_text += "\n\n(支持 Markdown 格式，发送 `/empty` 可清空协议)"

            prompt_msg = await conv.send_message(prompt_text, buttons=cancel_btn)

            new_value = await get_user_input_or_cancel(conv, prompt_msg.id)

            if not new_value:
                await safe_delete(prompt_msg)
                return

            await safe_delete(prompt_msg)

            if new_value.strip() == "/empty" and field_type == 'tos':
                new_value = ""

            result = await settings_service.update_server_field(server_id, db_field, new_value)

            if result.success:
                await event.answer("更新成功")

                panel = await settings_service.get_server_detail_panel(server_id)
                await event.edit(panel.message, buttons=panel.keyboard)
            else:
                await event.answer(f"更新失败: {result.message}", alert=True)

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。", alert=True)
    except asyncio.TimeoutError:
        await event.answer("操作超时", alert=True)
    except Exception as e:
        logger.error(f"Edit server error: {e}")
        await event.answer("发生错误，请重试", alert=True)

@TelethonClientWarper.handler(events.CallbackQuery(data=b'srv_edit_cancel'))
async def srv_edit_cancel_handler(app: FastAPI, event: events.CallbackQuery.Event) -> None:
    """取消编辑"""
    await event.answer("已取消编辑")
    await event.delete()

@TelethonClientWarper.handler(events.CallbackQuery(pattern=b'srv_edit_mapping_(\\d+)'))
@provide_db_session
@require_admin
async def srv_edit_mapping_handler(app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession) -> None:
    """编辑服务器路径映射"""
    server_id = int(event.pattern_match.group(1).decode()) # type: ignore
    chat_id = event.chat_id
    client = app.state.telethon_client.client
    settings_service = SettingsServices(app, session)

    msg_text = textwrap.dedent("""
        **✏️ 编辑路径映射**
        
        请输入映射规则，格式为：`/远程路径:/本地路径`
        一行一条规则。
        
        例如：
        `/media/tv:/mnt/share/tv`
        `/media/movies:/mnt/share/movies`
        
        发送 `/empty` 可清空所有映射。
        发送 `/cancel` 取消。
    """)

    try:
        async with client.conversation(chat_id, timeout=120) as conv:
            cancel_btn = [Button.inline("取消", b"srv_edit_cancel")]
            prompt_msg = await conv.send_message(msg_text, buttons=cancel_btn)

            input_val = await get_user_input_or_cancel(conv, prompt_msg.id)

            if not input_val:
                await safe_delete(prompt_msg)
                return

            await safe_delete(prompt_msg)

            if input_val.strip() == "/empty":
                mappings = {}
            else:
                mappings = {}
                lines = input_val.strip().split('\n')
                for line in lines:
                    if ':' in line:
                        parts = line.split(':', 1) # 只分割第一个冒号
                        remote = parts[0].strip()
                        local = parts[1].strip()
                        if remote and local:
                            mappings[remote] = local

                if not mappings and input_val.strip() != "/empty":
                    await event.answer("格式错误，未识别到有效映射", alert=True)
                    return

            result = await settings_service.update_server_mapping(server_id, mappings)

            if result.success:
                await event.answer("更新成功")
                panel = await settings_service.get_server_detail_panel(server_id)
                await event.edit(panel.message, buttons=panel.keyboard)
            else:
                await event.answer(f"更新失败: {result.message}", alert=True)

    except errors.AlreadyInConversationError:
        await event.answer("⚠️ 错误：当前已有正在进行的会话。", alert=True)
    except asyncio.TimeoutError:
        await event.answer("操作超时", alert=True)
    except Exception as e:
        logger.error(f"Edit mapping error: {e}")
        await event.answer("发生错误，请重试", alert=True)
