import asyncio
import base64
import textwrap

import aiofiles.tempfile
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, errors, events

from bot.decorators import provide_db_session, require_admin
from bot.utils import safe_delete
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
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

MAX_FILE_SIZE = 20 * 1024 * 1024
INTRO_MSG = textwrap.dedent("""
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

async def run_subtitle_upload_flow(
    user_id: int,
    telethon_client: TelethonClientWarper,
    session: AsyncSession,
    radarr_clients: dict[int, RadarrClient],
    sonarr_clients: dict[int, SonarrClient]
):
    """运行上传字幕流程 (Conversation)"""
    chat_id = user_id
    subtitle_service = SubtitleService(session, radarr_clients, sonarr_clients)
    client: TelegramClient = telethon_client.client

    try:
        async with client.conversation(chat_id, timeout=300) as conv:
            intro_msg = await conv.send_message(INTRO_MSG)
            if isinstance(intro_msg, list):
                intro_msg = intro_msg[0]

            # Wait for file loop
            while True:
                response_msg = await conv.get_response()
                if response_msg.text and response_msg.text.startswith('/'):
                    await intro_msg.edit("❌ 检测到命令，已取消上传。")
                    return

                if not response_msg.file:
                    await intro_msg.edit("请发送一个带有文件的消息 (Zip 格式)，或发送 /cancel 取消。")
                    continue

                if not response_msg.file.name.lower().endswith('.zip'):
                    await intro_msg.edit("❌ 格式错误！仅支持 `.zip` 格式的压缩包，请重新发送。")
                    continue

                if response_msg.file.size > MAX_FILE_SIZE:
                    await intro_msg.edit(f"❌ 文件过大！最大支持 {MAX_FILE_SIZE // 1024 // 1024} MiB，请重新发送。")
                    continue

                # Valid file found
                break

            processing_msg = await intro_msg.edit("📥 正在接收并处理文件，请稍候...")

            async with aiofiles.tempfile.NamedTemporaryFile(suffix=".zip") as tmp_file:
                file_path = await response_msg.download_media(file=tmp_file.name)

                if not file_path:
                    await processing_msg.edit("❌ 文件下载失败，请重试。")
                    return

                result = await subtitle_service.handle_file_upload(user_id, file_path, response_msg.file.name)
                if result.success:
                    await processing_msg.edit(result.message)
                else:
                    await processing_msg.edit(f"❌ **上传失败**\n\n{result.message}")

    except errors.AlreadyInConversationError:
        await client.send_message(chat_id, "⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或发送 /cancel 指令。")
    except asyncio.TimeoutError:
        await client.send_message(chat_id, "⏳ 操作超时，字幕上传会话已结束。")
    except Exception as e:
        logger.error(f"Conversation error: {e}")
        await client.send_message(chat_id, f"❌ 发生未知错误: {str(e)}")
