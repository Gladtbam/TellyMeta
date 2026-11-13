import asyncio
import base64
import contextlib
import re
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
from core.database import async_session
from core.telegram_manager import TelethonClientWarper
from services.request_service import RequestService
from services.subtitle_service import SubtitleService

settings = get_settings()


@TelethonClientWarper.handler(events.CallbackQuery(data=b"req_cancel"))
async def request_cancel_handler(
    app: FastAPI, event: events.CallbackQuery.Event
) -> None:
    """求片-取消处理器"""
    await safe_delete(event)


@TelethonClientWarper.handler(events.CallbackQuery(pattern=b"^req_ap_(\\d+)$"))
@provide_db_session
@require_admin
async def request_approve_by_id_handler(
    app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession
) -> None:
    """求片-按请求 ID 批准处理器"""
    request_id = int(event.pattern_match.group(1).decode("utf-8"))  # type: ignore
    sender = await event.get_sender()
    approver_name = (
        getattr(sender, "first_name", None)
        or getattr(sender, "username", None)
        or "管理员"
    )

    request_service = RequestService(app, session)
    await event.answer("正在处理审批...", alert=False)

    result = await request_service.handle_approval_by_request_id(
        request_id, approver_name=approver_name
    )

    msg = await event.get_message()
    original_text = msg.text if msg else ""
    if result.success:
        new_text = original_text + f"\n\n✅ **已批准**: {result.message}"
        await event.edit(new_text, buttons=None)
    else:
        await event.answer(result.message, alert=True)
        # 若请求已不再处于 pending（例如失败释分、已撤销或已超时），移除操作按钮避免误点
        req = await request_service.request_repo.get_by_id(request_id)
        if req and req.status != "pending":
            await event.edit(
                original_text + f"\n\n⚠️ **{result.message}**",
                buttons=None,
            )


@TelethonClientWarper.handler(
    events.CallbackQuery(pattern=b"^req_ap_(\\d+)_([^_]+)_(\\d+)")
)
@provide_db_session
@require_admin
async def request_approve_handler(
    app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession
) -> None:
    """求片-批准处理器（兼容旧版回调格式）"""
    media_server_id = int(event.pattern_match.group(1).decode("utf-8"))  # type: ignore
    library_name_base64 = event.pattern_match.group(2).decode("utf-8")  # type: ignore
    media_id = int(event.pattern_match.group(3).decode("utf-8"))  # type: ignore

    library_name = base64.b64decode(library_name_base64.encode("utf-8")).decode("utf-8")

    request_service = RequestService(app, session)
    # Give admin immediate feedback
    await event.answer("正在添加中...", alert=False)

    result = await request_service.handle_approval(
        media_server_id, library_name, media_id
    )

    if result.success:
        # Edit the message to show approved status and remove buttons
        original_text = (await event.get_message()).text  # type: ignore
        new_text = original_text + f"\n\n✅ **已批准**: {result.message}"
        await event.edit(new_text, buttons=None)
    else:
        await event.answer(result.message, alert=True)


@TelethonClientWarper.handler(events.CallbackQuery(pattern=b"^req_deny_(\\d+)$"))
@provide_db_session
@require_admin
async def request_deny_handler(
    app: FastAPI, event: events.CallbackQuery.Event, session: AsyncSession
) -> None:
    """求片-拒绝处理器"""
    target_id = int(event.pattern_match.group(1).decode("utf-8"))  # type: ignore
    sender = await event.get_sender()
    approver_name = (
        getattr(sender, "first_name", None)
        or getattr(sender, "username", None)
        or "管理员"
    )

    request_service = RequestService(app, session)
    req = await request_service.request_repo.get_by_id(target_id)
    msg = await event.get_message()
    original_text = msg.text if msg else ""

    if not req:
        # 兼容旧版 req_deny_{user_id} 回调格式
        await event.edit(original_text + "\n\n❌ **已拒绝**", buttons=None)
        return

    result = await request_service.handle_rejection(
        target_id, approver_name=approver_name
    )
    if result.success:
        await event.edit(
            original_text + f"\n\n❌ **已拒绝**: {result.message}", buttons=None
        )
    else:
        await event.answer(result.message, alert=True)
        latest = await request_service.request_repo.get_by_id(target_id)
        if latest and latest.status != "pending":
            await event.edit(
                original_text + f"\n\n⚠️ **{result.message}**",
                buttons=None,
            )


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
    session: AsyncSession | None,
    radarr_clients: dict[int, RadarrClient],
    sonarr_clients: dict[int, SonarrClient],
    media_server_id: int,
) -> None:
    """运行上传字幕流程 (Conversation)，仅在所属媒体服务器已绑定的 Arr 实例中定位目标"""
    chat_id = user_id
    client: TelegramClient = telethon_client.client

    async def _run_with_session(active_session: AsyncSession) -> None:
        subtitle_service = SubtitleService(
            active_session, radarr_clients, sonarr_clients
        )

        try:
            async with client.conversation(chat_id, timeout=300) as conv:
                intro_msg = await conv.send_message(INTRO_MSG)
                if isinstance(intro_msg, list):
                    intro_msg = intro_msg[0]

                # Wait for file loop
                while True:
                    response_msg = await conv.get_response()
                    if response_msg.text and response_msg.text.startswith("/"):
                        await intro_msg.edit("❌ 检测到命令，已取消上传。")
                        return

                    if not response_msg.file:
                        await intro_msg.edit(
                            "请发送一个带有文件的消息 (Zip 格式)，或发送 /cancel 取消。"
                        )
                        continue

                    if not response_msg.file.name.lower().endswith(".zip"):
                        await intro_msg.edit(
                            "❌ 格式错误！仅支持 `.zip` 格式的压缩包，请重新发送。"
                        )
                        continue

                    if response_msg.file.size > MAX_FILE_SIZE:
                        await intro_msg.edit(
                            f"❌ 文件过大！最大支持 {MAX_FILE_SIZE // 1024 // 1024} MiB，请重新发送。"
                        )
                        continue

                    # Valid file found
                    break

                processing_msg = await intro_msg.edit(
                    "📥 正在接收并处理文件，请稍候..."
                )

                async with aiofiles.tempfile.NamedTemporaryFile(
                    suffix=".zip"
                ) as tmp_file:
                    file_path = await response_msg.download_media(file=tmp_file.name)

                    if not file_path:
                        await processing_msg.edit("❌ 文件下载失败，请重试。")
                        return

                    file_name = response_msg.file.name
                    target_arr_id: int | None = None

                    # 若多个已绑定实例均匹配同一外部媒体 ID，先让用户明确目标实例再写入
                    match = re.search(
                        r"^(tvdb|tmdb)-(\d+)\.zip$", file_name, re.IGNORECASE
                    )
                    if match:
                        m_type = match.group(1).lower()
                        m_id = int(match.group(2))
                        targets = await subtitle_service.find_matching_arr_targets(
                            media_server_id, m_type, m_id
                        )
                        if len(targets) > 1:
                            options_text = "\n".join(
                                f"{idx}. {name} (ID: {aid})"
                                for idx, (aid, name) in enumerate(targets, 1)
                            )
                            await processing_msg.edit(
                                f"⚠️ 多个已绑定的实例均匹配该媒体，请回复对应序号明确目标：\n\n"
                                f"{options_text}\n\n发送 `/cancel` 取消。"
                            )
                            while True:
                                choice_msg = await conv.get_response()
                                choice_text = (choice_msg.text or "").strip()
                                if choice_text.startswith("/"):
                                    await processing_msg.edit("❌ 已取消上传。")
                                    return
                                if choice_text.isdigit():
                                    idx = int(choice_text)
                                    if 1 <= idx <= len(targets):
                                        target_arr_id = targets[idx - 1][0]
                                        await processing_msg.edit(
                                            "📥 正在写入目标实例，请稍候..."
                                        )
                                        break
                                await conv.send_message(
                                    f"请输入有效序号（1-{len(targets)}），或发送 `/cancel` 取消。"
                                )

                    result = await subtitle_service.handle_file_upload(
                        user_id,
                        file_path,
                        file_name,
                        media_server_id=media_server_id,
                        target_arr_id=target_arr_id,
                    )
                    if result.success:
                        await processing_msg.edit(result.message)
                    else:
                        await processing_msg.edit(
                            f"❌ **上传失败**\n\n{result.message}"
                        )

                with contextlib.suppress(Exception):
                    await response_msg.delete()

        except errors.AlreadyInConversationError:
            await client.send_message(
                chat_id,
                "⚠️ 错误：当前已有正在进行的会话。\n请先完成它，或发送 /cancel 指令。",
            )
        except asyncio.TimeoutError:
            await client.send_message(chat_id, "⏳ 操作超时，字幕上传会话已结束。")
        except Exception as e:
            logger.error(f"Conversation error: {e}")
            await client.send_message(chat_id, f"❌ 发生未知错误: {str(e)}")

    if session is not None:
        await _run_with_session(session)
    else:
        async with async_session() as bg_session:
            await _run_with_session(bg_session)
