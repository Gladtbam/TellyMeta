import asyncio
from typing import NoReturn

from loguru import logger

from workers.mkv_utils import mkv_merge


async def mkv_merge_task(task_queue: asyncio.Queue) -> NoReturn:
    """异步处理 MKV 合并任务队列"""
    while True:
        episode_path = None
        try:
            episode_path = await task_queue.get()
            logger.info("正在处理 %s 以进行 MKV 合并", episode_path)
            await mkv_merge(episode_path)
        except Exception as e:
            logger.error("处理 MKV 合并任务时出错：%s", e)
        finally:
            if episode_path is not None:
                task_queue.task_done()
                logger.info("%s 的 MKV 合并任务已完成", episode_path)
