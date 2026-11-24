import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, NoReturn

from loguru import logger

from services.notification_renderer import MkvProcessFailedPayload
from services.notification_service import NotificationService
from workers.mkv_utils import mkv_merge


class _MkvQueueItem(NamedTuple):
    """标准化的 MKV 队列任务上下文（保留 4 元组拆包兼容性）。"""

    episode_path: Path | None
    notify_topic_id: int | None
    server_id: int | None
    server_name: str


async def _notify_admin_mkv_failure(
    app_state: Any,
    episode_path: Path,
    notify_topic_id: int | None,
    error_message: str,
    *,
    server_id: int | None = None,
    server_name: str = "",
) -> None:
    """通过统一通知管线向运维告警通道发射 MKV 处理失败事件。

    Args:
        app_state: FastAPI 应用 ``state`` 对象或包装应用实例。
        episode_path: 触发失败的本地 MKV 文件路径。
        notify_topic_id: 兼容保留的旧版话题参数（统一管线改由 ``ops_alert`` 路由解析）。
        error_message: 失败诊断详情。
        server_id: 可选来源 Arr 服务器 ID（用于匹配单服覆写路由）。
        server_name: 可选来源服务器名称。
    """
    _ = notify_topic_id
    if app_state is None:
        return
    app_wrapper = (
        app_state if hasattr(app_state, "state") else SimpleNamespace(state=app_state)
    )
    try:
        # 绑定 server_id 确保遵循该服务器的 ops_alert 覆盖路由，并以服务器前缀隔离冷却键
        source_scope = str(server_id) if server_id is not None else "global"
        await NotificationService(app_wrapper).emit(
            MkvProcessFailedPayload(
                file_path=str(episode_path),
                error_message=error_message,
                server_name=server_name,
            ),
            server_id=server_id,
            source_key=f"mkv:{source_scope}:{episode_path}",
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("发射 MKV 失败告警通知时出错: {}", exc)


def _unpack_mkv_queue_item(item: Any) -> _MkvQueueItem:
    """解析 MKV 任务队列元素，兼容 Path、二元组及含服务器上下文的四元组。"""
    if isinstance(item, tuple):
        if len(item) >= 4:
            return _MkvQueueItem(item[0], item[1], item[2], str(item[3] or ""))
        if len(item) == 3:
            return _MkvQueueItem(item[0], item[1], item[2], "")
        if len(item) == 2:
            return _MkvQueueItem(item[0], item[1], None, "")
        if len(item) == 1:
            return _MkvQueueItem(item[0], None, None, "")
        return _MkvQueueItem(None, None, None, "")
    return _MkvQueueItem(item, None, None, "")


async def mkv_merge_task(task_queue: asyncio.Queue, app_state: Any = None) -> NoReturn:
    """异步处理 MKV 合并任务队列"""
    while True:
        item = None
        episode_path: Path | None = None
        notify_topic_id: int | None = None
        server_id: int | None = None
        server_name: str = ""
        try:
            item = await task_queue.get()
            episode_path, notify_topic_id, server_id, server_name = (
                _unpack_mkv_queue_item(item)
            )

            if episode_path is None:
                continue

            logger.info("正在处理 {} 以进行 MKV 合并", episode_path)
            ok, err_msg = await mkv_merge(episode_path)
            if not ok and err_msg:
                await _notify_admin_mkv_failure(
                    app_state,
                    episode_path,
                    notify_topic_id,
                    err_msg,
                    server_id=server_id,
                    server_name=server_name,
                )
        except Exception as e:
            logger.error("处理 MKV 合并任务时出错：{}", e)
            if episode_path is not None:
                await _notify_admin_mkv_failure(
                    app_state,
                    episode_path,
                    notify_topic_id,
                    str(e),
                    server_id=server_id,
                    server_name=server_name,
                )
        finally:
            if item is not None:
                task_queue.task_done()
                logger.info("{} 的 MKV 合并任务已完成", episode_path)
