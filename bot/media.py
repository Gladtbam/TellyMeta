import asyncio
import base64
import textwrap

import aiofiles.tempfile
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, errors, events

from bot.decorators import provide_db_session, require_admin
from bot.utils import get_user_input_or_cancel, safe_delete, safe_respond
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.telegram_repo import TelegramRepository
from services.request_service import RequestService
from services.subtitle_service import SubtitleService

settings = get_settings()


@TelethonClientWarper.handler(events.CallbackQuery(data=b'req_cancel'))
async def request_cancel_handler(app: FastAPI, event: events.CallbackQuery.Event) -> None:
    """求片-取消处理器"""
    await safe_delete(event)

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
async def start_request_conversation_handler(
    app: FastAPI,
    event: events.CallbackQuery.Event,
    session: AsyncSession
) -> None:
    """开始求片处理器 (Conversation Mode)"""
    user_id = int(event.pattern_match.group(1).decode('utf-8')) # type: ignore
    chat_id = event.chat_id
    request_service = RequestService(app, session)
    telegram_repo = TelegramRepository(session)
    client = app.state.telethon_client.client

    request_cost = int(await telegram_repo.get_renew_score() * 0.1)
    # 检查权限
    # start_request_flow 将检查权限并返回库按钮
    result = await request_service.start_request_flow(user_id, request_cost)

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
                await safe_delete(press_event)
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
                await safe_delete(query_prompt)
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
                await safe_delete(sel_event)
                return

            # 解析选择: req_sel_{lib_b64}_{media_id}
            sel_parts = sel_data.split('_')
            media_id = int(sel_parts[3])

            await sel_event.answer("获取详情中...", alert=False)
            preview_result = await request_service.process_media_selection(user_id, library_name, media_id)
            if not preview_result.success:
                await sel_event.edit(preview_result.message, alert=False)
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
                await safe_delete(confirm_event)
                return

            if confirm_data.startswith('req_submit_'):
                await confirm_event.answer("正在提交...", alert=False)
                final_result = await request_service.submit_final_request(user_id, library_name, media_id, request_cost)

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
            if response_msg.file.size and response_msg.file.size > 20 * 1024 * 1024:
                await processing_msg.edit("❌ 文件过大！最大支持 20 MiB，请重新发送。")
                return
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
