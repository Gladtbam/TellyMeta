import asyncio
from pathlib import Path
from typing import Any, NoReturn

from loguru import logger

from core.config import get_settings
from workers.mkv_utils import mkv_merge

settings = get_settings()


async def _notify_admin_mkv_failure(
    app_state: Any,
    episode_path: Path,
    notify_topic_id: int | None,
    error_message: str,
) -> None:
    """向管理员话题发送 MKV 处理失败的可诊断告警"""
    telethon_client = getattr(app_state, "telethon_client", None) if app_state else None
    if not telethon_client:
        return

    target_chat = settings.telegram_chat_id
    target_reply = None
    if notify_topic_id and notify_topic_id < 0:
        target_chat = notify_topic_id
    elif notify_topic_id and notify_topic_id > 0:
        target_chat = settings.telegram_chat_id
        target_reply = notify_topic_id

    text = (
        f"⚠️ **MKV 自动清理失败（原文件保持完整）**\n\n"
        f"**文件路径**: `{episode_path}`\n"
        f"**诊断信息**: {error_message}"
    )
    try:
        await telethon_client.send_message(
            chat_id=target_chat,
            message=text,
            reply_to=target_reply,
        )
    except Exception as e:
        logger.warning("发送 MKV 失败告警给管理员时出错: {}", e)


async def mkv_merge_task(task_queue: asyncio.Queue, app_state: Any = None) -> NoReturn:
    """异步处理 MKV 合并任务队列"""
    while True:
        item = None
        episode_path: Path | None = None
        notify_topic_id: int | None = None
        try:
            item = await task_queue.get()
            if isinstance(item, tuple):
                episode_path, notify_topic_id = item
            else:
                episode_path = item

            if episode_path is None:
                continue

            logger.info("正在处理 {} 以进行 MKV 合并", episode_path)
            ok, err_msg = await mkv_merge(episode_path)
            if not ok and err_msg:
                await _notify_admin_mkv_failure(
                    app_state, episode_path, notify_topic_id, err_msg
                )
        except Exception as e:
            logger.error("处理 MKV 合并任务时出错：{}", e)
            if episode_path is not None:
                await _notify_admin_mkv_failure(
                    app_state, episode_path, notify_topic_id, str(e)
                )
        finally:
            if item is not None:
                task_queue.task_done()
                logger.info("{} 的 MKV 合并任务已完成", episode_path)
