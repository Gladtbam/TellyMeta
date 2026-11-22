import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

from loguru import logger

from services.notification_renderer import MkvProcessFailedPayload
from services.notification_service import NotificationService
from workers.mkv_utils import mkv_merge


async def _notify_admin_mkv_failure(
    app_state: Any,
    episode_path: Path,
    notify_topic_id: int | None,
    error_message: str,
    *,
    server_name: str = "",
) -> None:
    """通过统一通知管线向运维告警通道发射 MKV 处理失败事件。

    Args:
        app_state: FastAPI 应用 ``state`` 对象或包装应用实例。
        episode_path: 触发失败的本地 MKV 文件路径。
        notify_topic_id: 兼容保留的旧版话题参数（统一管线改由 ``ops_alert`` 路由解析）。
        error_message: 失败诊断详情。
        server_name: 可选来源服务器名称。
    """
    _ = notify_topic_id
    if app_state is None:
        return
    app_wrapper = (
        app_state if hasattr(app_state, "state") else SimpleNamespace(state=app_state)
    )
    try:
        await NotificationService(app_wrapper).emit(
            MkvProcessFailedPayload(
                file_path=str(episode_path),
                error_message=error_message,
                server_name=server_name,
            ),
            source_key=f"mkv:{episode_path}",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("发射 MKV 失败告警通知时出错: {}", e)


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
